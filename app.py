import os
import json
import re
import calendar
from datetime import datetime, timedelta, timezone
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# ===== 環境變數檢查 =====
required_env_vars = [
    "LINE_CHANNEL_ACCESS_TOKEN",
    "LINE_CHANNEL_SECRET",
    "SHEET_ID",
    "GOOGLE_CREDENTIALS"
]
missing = [var for var in required_env_vars if not os.environ.get(var)]
if missing:
    raise EnvironmentError(f"缺少環境變數: {', '.join(missing)}")

# ===== 時區設定 (台灣時間 UTC+8) =====
TAIPEI_TZ = timezone(timedelta(hours=8))

def now_taipei():
    return datetime.now(TAIPEI_TZ)

# ===== LINE =====
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# ===== Google Sheet =====
SHEET_ID = os.environ.get("SHEET_ID")

scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]

creds_json = json.loads(os.environ.get("GOOGLE_CREDENTIALS"))
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_json, scope)
client = gspread.authorize(creds)

_user_name_cache = {}

def get_line_user_name(user_id):
    if user_id in _user_name_cache:
        return _user_name_cache[user_id]
    try:
        profile = line_bot_api.get_profile(user_id)
        name = profile.display_name
        _user_name_cache[user_id] = name
        return name
    except Exception:
        return user_id

def get_user_worksheet(user_id, user_name=None):
    if user_name is None:
        user_name = get_line_user_name(user_id)
    sheet_title = f"{user_name} ({user_id})"[:100]
    try:
        worksheet = client.open_by_key(SHEET_ID).worksheet(sheet_title)
    except gspread.WorksheetNotFound:
        worksheet = client.open_by_key(SHEET_ID).add_worksheet(
            title=sheet_title, rows=1000, cols=9
        )
        worksheet.append_row(["日期","狀態","地點","工時","加班","加班費","出差費","收入","用戶名稱"])
    return worksheet

# ===== 薪資設定 =====
BASE_SALARY = 43000                # 底薪（不含全勤）
FULL_ATTENDANCE_BONUS = 2000       # 全勤獎金
OVERTIME_HOURLY_RATE = 187.5       # 加班費基數（每小時）

TRAVEL_FEES = {
    "台中": 200,
    "台南": 400,
    "高雄": 500
}

def get_days_in_month(year, month):
    return calendar.monthrange(year, month)[1]

def get_daily_wage(date):
    year = date.year
    month = date.month
    days = get_days_in_month(year, month)
    return BASE_SALARY / days

def parse_time_str(t):
    t = t.strip()
    if len(t) == 3:
        t = "0" + t
    hour = int(t[:2])
    minute = int(t[2:])
    return hour + minute / 60.0

def normalize_location(loc):
    loc = loc.replace("市", "").replace(" ", "").replace("臺", "台")
    if loc in TRAVEL_FEES:
        return loc
    return loc

def parse_message(text):
    text = text.strip()
    if "請假" in text:
        return {"status": "請假", "leave_type": "事假"}

    try:
        text = text.replace("～", "~").replace("-", "~")
        match = re.search(r"(\D+)\s*(\d{3,4})~(\d{3,4})", text)
        if not match:
            return {"status": "錯誤"}

        location_raw = match.group(1).strip()
        location = normalize_location(location_raw)
        start = parse_time_str(match.group(2))
        end = parse_time_str(match.group(3))

        return {"status": "上班", "location": location, "start": start, "end": end}
    except:
        return {"status": "錯誤"}

def calculate_work(data, date):
    if data["status"] == "請假":
        return {
            "work_hours": 0,
            "overtime": 0,
            "overtime_pay": 0,
            "travel_fee": 0,
            "income": 0
        }

    daily_wage = get_daily_wage(date)
    hourly_wage = daily_wage / 8.0

    start = data["start"]
    end = data["end"]

    work_hours = end - start
    if start < 13.0 and end > 12.0:
        work_hours -= 1.0
    work_hours = max(work_hours, 0)

    overtime = max(work_hours - 8, 0)
    normal_hours = min(work_hours, 8)

    normal_pay = (normal_hours / 8.0) * daily_wage

    overtime_pay = 0.0
    if overtime > 0:
        first2 = min(overtime, 2)
        overtime_pay += first2 * OVERTIME_HOURLY_RATE * 1.34
        if overtime > 2:
            overtime_pay += (overtime - 2) * OVERTIME_HOURLY_RATE * 1.67

    if date.weekday() == 5:  # 星期六
        normal_pay *= 2
        overtime_pay *= 2

    travel_fee = TRAVEL_FEES.get(data.get("location", ""), 0)
    income = normal_pay + overtime_pay + travel_fee

    overtime_pay = int(round(overtime_pay))
    income = int(round(income))

    return {
        "work_hours": round(work_hours, 1),
        "overtime": round(overtime, 1),
        "overtime_pay": overtime_pay,
        "travel_fee": travel_fee,
        "income": income
    }

def append_or_update_sheet(user_id, user_name, date_str, data, calc):
    worksheet = get_user_worksheet(user_id, user_name)
    records = worksheet.get_all_records()
    target_row = None
    for idx, rec in enumerate(records, start=2):
        if rec.get("日期") == date_str:
            target_row = idx
            break

    row_data = [
        date_str,
        data.get("status", ""),
        data.get("location", ""),
        calc.get("work_hours", 0),
        calc.get("overtime", 0),
        calc.get("overtime_pay", 0),
        calc.get("travel_fee", 0),
        calc.get("income", 0),
        user_name
    ]

    if target_row:
        worksheet.update(f"A{target_row}:I{target_row}", [row_data])
    else:
        worksheet.append_row(row_data)

def generate_monthly_report(user_id, user_name, year=None, month=None, cutoff_date=None):
    worksheet = get_user_worksheet(user_id, user_name)
    records = worksheet.get_all_records()

    now = now_taipei()
    if year is None or month is None:
        year = now.year
        month = now.month
    start_date = datetime(year, month, 1, tzinfo=TAIPEI_TZ)

    # 確定該月的總天數（用於計算日薪）
    total_days_in_month = calendar.monthrange(year, month)[1]
    daily_wage = BASE_SALARY / total_days_in_month

    # 設定迴圈的結束日期
    if cutoff_date is None:
        # 整月：結束日期為下個月1號
        if month == 12:
            end_date = datetime(year+1, 1, 1, tzinfo=TAIPEI_TZ)
        else:
            end_date = datetime(year, month+1, 1, tzinfo=TAIPEI_TZ)
    else:
        # 有截止日，且必須與當月相符，否則視為整月
        if cutoff_date.year != year or cutoff_date.month != month:
            if month == 12:
                end_date = datetime(year+1, 1, 1, tzinfo=TAIPEI_TZ)
            else:
                end_date = datetime(year, month+1, 1, tzinfo=TAIPEI_TZ)
        else:
            # 截止日為當天，結束日期設為隔天，但不超過當月最後一天+1
            next_day = cutoff_date + timedelta(days=1)
            if month == 12:
                month_end = datetime(year+1, 1, 1, tzinfo=TAIPEI_TZ)
            else:
                month_end = datetime(year, month+1, 1, tzinfo=TAIPEI_TZ)
            end_date = min(next_day, month_end)

    total_overtime_hours = 0
    total_overtime_pay = 0
    total_travel_fee = 0
    leave_days = 0
    worked_days = 0
    has_leave = False

    record_map = {r["日期"]: r for r in records if r.get("日期")}

    current = start_date
    while current < end_date:
        # 安全防護：如果日期已經超出當月，立即停止（避免跨月）
        if current.year != year or current.month != month:
            break

        date_str = current.strftime("%Y-%m-%d")
        is_weekday = current.weekday() < 5  # 週一至週五

        if date_str in record_map:
            row = record_map[date_str]
            status = row["狀態"]
            if status == "請假":
                has_leave = True
                leave_days += 1
            else:
                if is_weekday:
                    worked_days += 1
                total_overtime_hours += float(row.get("加班", 0))
                total_overtime_pay += int(row.get("加班費", 0))
                total_travel_fee += int(row.get("出差費", 0))
        else:
            # 未打卡日：如果是平日，視為正常上下班，計入實際出勤
            if is_weekday:
                worked_days += 1

        current += timedelta(days=1)

    # 底薪 = 實際出勤天數 × 日薪
    base_salary = worked_days * daily_wage

    # 全勤獎金（無請假才給）
    if not has_leave:
        base_salary += FULL_ATTENDANCE_BONUS

    # 總收入 = 底薪 + 加班費 + 出差費
    total_income = base_salary + total_overtime_pay + total_travel_fee
    total_income = int(round(total_income))

    # 建構訊息
    msg_lines = []
    if cutoff_date is None or (cutoff_date.year == year and cutoff_date.month == month and cutoff_date.day == total_days_in_month):
        msg_lines.append(f"📅 {year}年{month}月 薪資總結")
    else:
        msg_lines.append(f"📅 {year}年{month}月 薪資總結 (累積至{cutoff_date.month}月{cutoff_date.day}日)")

    msg_lines.append(f"💰 總收入：{total_income} 元")
    msg_lines.append(f"🏆 全勤獎金 {'✅' if not has_leave else '❌'}")
    if total_overtime_hours > 0:
        msg_lines.append(f"⏱ 加班總時數：{total_overtime_hours:.1f} 小時")
        msg_lines.append(f"💰 加班費總計：{total_overtime_pay} 元")
    if total_travel_fee > 0:
        msg_lines.append(f"🚗 出差費總計：{total_travel_fee} 元")
    msg_lines.append(f"📊 出勤狀況：")
    msg_lines.append(f"   • 實際出勤：{worked_days} 天")
    msg_lines.append(f"   • 請假：{leave_days} 天")

    return "\n".join(msg_lines), total_income

def generate_yearly_report(user_id, user_name, year=None):
    if year is None:
        year = now_taipei().year
    worksheet = get_user_worksheet(user_id, user_name)
    records = worksheet.get_all_records()

    months_with_data = set()
    for rec in records:
        if rec.get("日期"):
            try:
                d = datetime.strptime(rec["日期"], "%Y-%m-%d")
                if d.year == year:
                    months_with_data.add(d.month)
            except:
                pass

    monthly_data = []
    total_year = 0

    for month in sorted(months_with_data):
        try:
            report_str, total = generate_monthly_report(user_id, user_name, year, month)
            monthly_data.append((month, total))
            total_year += total
        except Exception:
            pass

    msg_lines = []
    msg_lines.append(f"📅 {year}年 年度薪資總結")
    if not monthly_data:
        msg_lines.append("   該年度尚無任何打卡或請假記錄")
    else:
        for month, income in monthly_data:
            msg_lines.append(f"   {month}月：{income} 元")
        msg_lines.append(f"💰 年度總收入：{total_year} 元")
    return "\n".join(msg_lines), total_year

def parse_command(text):
    text = text.strip()
    if text == "規則":
        return ("rule", None)
    if text in ["今年", "本年"]:
        return ("yearly", None)
    if text == "本月":
        return ("monthly", None)

    patterns = [
        r"(\d{4})年(\d{1,2})月",
        r"(\d{4})-(\d{1,2})",
        r"(\d{4})/(\d{1,2})"
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            year = int(m.group(1))
            month = int(m.group(2))
            if 1 <= month <= 12:
                return ("monthly", (year, month))
    return (None, None)

app = Flask(__name__)

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = event.message.text
    user_id = event.source.user_id
    user_name = get_line_user_name(user_id)

    cmd, params = parse_command(text)
    if cmd == "rule":
        rule_text = (
            "📌 使用規則\n"
            "1️⃣ 上班打卡：地點 開始~結束\n"
            "   例：台中 830~1730\n"
            "2️⃣ 請假：請假\n"
            "3️⃣ 查詢本月：本月\n"
            "4️⃣ 查詢指定月份：2026年2月 或 2026-02\n"
            "5️⃣ 查詢年度：今年 / 本年\n"
            "6️⃣ 薪資計算：\n"
            "   - 底薪43000元，全勤2000元（無請假即加發）\n"
            "   - 加班費187.5元/小時，前2小時1.34倍，其後1.67倍，星期六2倍\n"
            "   - 平日未打卡自動視為正常上下班（給付日薪）\n"
            "   - 週末未打卡亦自動給薪\n"
            "   - 每人獨立工作表，資料不混雜"
        )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=rule_text))
        return

    if cmd == "yearly":
        try:
            msg, total = generate_yearly_report(user_id, user_name)
            if len(msg) > 1900:
                msg = f"📅 年度薪資總結\n💰 總收入：{total} 元\n（詳細資料請至工作表查看）"
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))
        except Exception as e:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"產生年度報表錯誤: {str(e)}"))
        return

    if cmd == "monthly":
        try:
            if params is None:
                # 本月查詢：只累積到今天
                now = now_taipei()
                cutoff = now
                year, month = now.year, now.month
                msg, total = generate_monthly_report(user_id, user_name, year, month, cutoff_date=cutoff)
            else:
                year, month = params
                msg, total = generate_monthly_report(user_id, user_name, year, month)
            if len(msg) > 1900:
                msg = f"📅 {year if params else '本月'} 薪資總結\n💰 總收入：{total} 元\n（詳細資料請至工作表查看）"
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))
        except Exception as e:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"產生報表錯誤: {str(e)}"))
        return

    # 打卡處理
    try:
        data = parse_message(text)
        if data["status"] == "錯誤":
            reply = "格式錯誤：請輸入「地點 開始時間~結束時間」，例如「台中 830~1730」或「請假」\n輸入「規則」查看完整使用說明。"
        else:
            today = now_taipei()
            date_str = today.strftime("%Y-%m-%d")
            calc = calculate_work(data, today)
            append_or_update_sheet(user_id, user_name, date_str, data, calc)
            reply = (f"📍 地點：{data.get('location', '')}\n"
                     f"工時：{calc['work_hours']} 小時\n"
                     f"加班：{calc['overtime']} 小時\n"
                     f"加班費：{calc['overtime_pay']} 元\n"
                     f"出差費：{calc['travel_fee']} 元\n"
                     f"今日收入：{calc['income']} 元")
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
    except Exception as e:
        error_reply = f"處理訊息時發生錯誤: {str(e)}"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=error_reply))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)