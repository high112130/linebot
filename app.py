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

_sheet_cache = None

def get_sheet():
    global _sheet_cache
    if _sheet_cache is None:
        sheet_name = now_taipei().strftime("%Y-%m")
        try:
            _sheet_cache = client.open_by_key(SHEET_ID).worksheet(sheet_name)
        except gspread.WorksheetNotFound:
            _sheet_cache = client.open_by_key(SHEET_ID).add_worksheet(
                title=sheet_name, rows=1000, cols=10
            )
            _sheet_cache.append_row(["user_id","日期","狀態","地點","工時","加班","加班費","出差費","收入"])
    return _sheet_cache

# ===== 薪資設定 =====
MONTHLY_SALARY = 45000          # 固定月薪
OVERTIME_HOURLY_RATE = 187.5    # 每小時加班費基數
FULL_ATTENDANCE_BONUS = 2000    # 全勤獎金

TRAVEL_FEES = {
    "台中": 200,
    "台南": 400,
    "高雄": 500
}

# ===== 輔助函數 =====
def get_days_in_month(year, month):
    """取得當月總天數"""
    return calendar.monthrange(year, month)[1]

def get_daily_wage(date):
    """計算指定日期所屬月份的日薪 (月薪 ÷ 當月總天數)"""
    year = date.year
    month = date.month
    days = get_days_in_month(year, month)
    return MONTHLY_SALARY / days

def parse_time_str(t):
    """將四位數字字串轉為小時數，例如 '830' -> 8.5"""
    t = t.strip()
    if len(t) == 3:
        t = "0" + t
    hour = int(t[:2])
    minute = int(t[2:])
    return hour + minute / 60.0

def normalize_location(loc):
    """地點正規化"""
    loc = loc.replace("市", "").replace(" ", "").replace("臺", "台")
    if loc in TRAVEL_FEES:
        return loc
    return loc

# ===== 解析訊息 =====
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

# ===== 計算薪資（日薪制 + 固定加班費率）=====
def calculate_work(data, date):
    """
    計算當日收入
    - 正常工時工資：按實際工時比例給日薪（週六加倍）
    - 加班費：每小時 187.5 元，前2小時1.34倍，其後1.67倍（週六再乘2）
    """
    if data["status"] == "請假":
        return {
            "work_hours": 0,
            "overtime": 0,
            "overtime_pay": 0,
            "travel_fee": 0,
            "income": 0
        }

    # 取得當日基本日薪（未乘倍率）
    daily_wage = get_daily_wage(date)
    hourly_wage = daily_wage / 8.0   # 時薪（用於比例計算）

    start = data["start"]
    end = data["end"]

    # 計算實際工時，扣除午休（若跨過12:00~13:00）
    work_hours = end - start
    if start < 13.0 and end > 12.0:
        work_hours -= 1.0
    work_hours = max(work_hours, 0)

    overtime = max(work_hours - 8, 0)
    normal_hours = min(work_hours, 8)

    # 正常工時工資（按比例給日薪）
    normal_pay = (normal_hours / 8.0) * daily_wage

    # 加班費（以固定187.5為基數）
    overtime_pay = 0
    if overtime > 0:
        first2 = min(overtime, 2)
        overtime_pay += first2 * OVERTIME_HOURLY_RATE * 1.34
        if overtime > 2:
            overtime_pay += (overtime - 2) * OVERTIME_HOURLY_RATE * 1.67

    # 星期六（weekday=5）加成
    if date.weekday() == 5:
        normal_pay *= 2
        overtime_pay *= 2

    travel_fee = TRAVEL_FEES.get(data.get("location", ""), 0)
    income = normal_pay + overtime_pay + travel_fee

    return {
        "work_hours": round(work_hours, 1),
        "overtime": round(overtime, 1),
        "overtime_pay": round(overtime_pay, 0),
        "travel_fee": travel_fee,
        "income": round(income, 0)
    }

# ===== 寫入/更新 Sheet =====
def append_or_update_sheet(user_id, date_str, data, calc):
    sheet = get_sheet()
    records = sheet.get_all_records()
    target_row = None
    for idx, rec in enumerate(records, start=2):
        if rec.get("user_id") == user_id and rec.get("日期") == date_str:
            target_row = idx
            break

    row_data = [
        user_id, date_str, data.get("status", ""), data.get("location", ""),
        calc.get("work_hours", 0), calc.get("overtime", 0),
        calc.get("overtime_pay", 0), calc.get("travel_fee", 0),
        calc.get("income", 0)
    ]

    if target_row:
        sheet.update(f"A{target_row}:I{target_row}", [row_data])
    else:
        sheet.append_row(row_data)

# ===== 計算當月應工作日（週一至週五）=====
def get_workdays_in_month(year, month):
    first_day = datetime(year, month, 1, tzinfo=TAIPEI_TZ)
    if month == 12:
        last_day = datetime(year+1, 1, 1, tzinfo=TAIPEI_TZ) - timedelta(days=1)
    else:
        last_day = datetime(year, month+1, 1, tzinfo=TAIPEI_TZ) - timedelta(days=1)
    workdays = 0
    current = first_day
    while current <= last_day:
        if current.weekday() < 5:  # 週一~週五
            workdays += 1
        current += timedelta(days=1)
    return workdays

# ===== 月報表 =====
def generate_monthly_report(user_id):
    sheet = get_sheet()
    records = sheet.get_all_records()
    records = [r for r in records if r["user_id"] == user_id]
    record_map = {r["日期"]: r for r in records}

    now = now_taipei()
    year = now.year
    month = now.month
    start_date = datetime(year, month, 1, tzinfo=TAIPEI_TZ)
    if month == 12:
        end_date = datetime(year+1, 1, 1, tzinfo=TAIPEI_TZ)
    else:
        end_date = datetime(year, month+1, 1, tzinfo=TAIPEI_TZ)

    total_income = 0
    result = []
    worked_days = 0      # 實際有上班的天數（非請假且有打卡）
    has_leave = False    # 是否有任何請假記錄

    current = start_date
    while current < end_date:
        date_str = current.strftime("%Y-%m-%d")
        daily_wage = get_daily_wage(current)   # 該日基本日薪

        if date_str in record_map:
            row = record_map[date_str]
            income = float(row["收入"])
            status = row["狀態"]
            if status == "請假":
                has_leave = True
                result.append(f"{date_str}：請假 (扣薪)")
                # 請假不計收入，但已有記錄中收入為0
            else:
                worked_days += 1
                result.append(f"{date_str}：{int(income)} 元")
            total_income += income
        else:
            # 未打卡日：僅週一至週五自動給日薪（視為正常出勤），週六日不給
            if current.weekday() < 5:   # 週一~週五
                total_income += daily_wage
                result.append(f"{date_str}：{int(daily_wage)} 元 (自動給薪)")
            else:
                result.append(f"{date_str}：未打卡 (週末不計薪)")

        current += timedelta(days=1)

    # 全勤獎金：無請假記錄 且 實際上班天數 >= 當月應工作日數
    total_workdays = get_workdays_in_month(year, month)
    if not has_leave and worked_days >= total_workdays:
        total_income += FULL_ATTENDANCE_BONUS
        result.append(f"\n🏆 全勤獎金 +{FULL_ATTENDANCE_BONUS} 元")

    result.append(f"\n💰 本月總收入：{int(total_income)} 元")
    return result, int(total_income)

# ===== Flask =====
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

    if text == "本月":
        try:
            report_lines, total = generate_monthly_report(user_id)
            msg = "📅 本月薪資明細\n" + "\n".join(report_lines)
            if len(msg) > 1900:
                msg = f"📅 本月薪資總結\n💰 總收入：{total} 元\n（詳細明細過長，請至工作表查看）"
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))
        except Exception as e:
            error_msg = f"產生報表時發生錯誤: {str(e)}"
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=error_msg))
        return

    # 打卡訊息處理
    try:
        data = parse_message(text)
        if data["status"] == "錯誤":
            reply = "格式錯誤：請輸入「地點 開始時間~結束時間」，例如「台中 830~1730」或「請假」"
        else:
            today = now_taipei()
            date_str = today.strftime("%Y-%m-%d")
            calc = calculate_work(data, today)
            append_or_update_sheet(user_id, date_str, data, calc)
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
    app.run()