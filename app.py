import os
import json
import re
from datetime import datetime, timedelta
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import gspread
from oauth2client.service_account import ServiceAccountCredentials

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

# ===== 薪資設定 =====
BASE_SALARY = 28000
NEWBIE_ALLOWANCE = 12000
RENT_ALLOWANCE = 3000
FULL_ATTENDANCE_BONUS = 2000

TRAVEL_FEES = {
    "台中": 200,
    "台南": 400,
    "高雄": 500
}

# ===== Flask =====
app = Flask(__name__)

# ===== 取得當月 Sheet =====
def get_sheet():
    now = datetime.now()
    sheet_name = now.strftime("%Y-%m")
    try:
        return client.open_by_key(SHEET_ID).worksheet(sheet_name)
    except:
        sheet = client.open_by_key(SHEET_ID).add_worksheet(
            title=sheet_name, rows="1000", cols="10"
        )
        sheet.append_row(["user_id","日期","狀態","地點","工時","加班","加班費","出差費","收入"])
        return sheet

# ===== 解析訊息 =====
def parse_message(text):
    text = text.strip()
    if "請假" in text:
        return {"status": "請假"}

    try:
        text = text.replace("～", "~").replace("-", "~")
        match = re.search(r"(\D+)\s*(\d{3,4})~(\d{3,4})", text)
        if not match:
            return {"status": "錯誤"}

        location = match.group(1).strip()
        start = float(match.group(2)) / 100
        end = float(match.group(3)) / 100

        return {"status": "上班", "location": location, "start": start, "end": end}
    except:
        return {"status": "錯誤"}

# ===== 計算薪資 =====
def calculate_work(data):
    # 月薪不含全勤獎金
    total_salary = BASE_SALARY + NEWBIE_ALLOWANCE + RENT_ALLOWANCE
    daily_base = total_salary / 22  # 假設22個工作日
    hourly_wage = daily_base / 8

    if data["status"] == "請假":
        return {"work_hours": 0, "overtime": 0, "overtime_pay": 0,
                "travel_fee": 0, "income": daily_base}

    start = data["start"]
    end = data["end"]
    work_hours = max(end - start - 1, 0)  # 扣午休1小時
    overtime = max(work_hours - 8, 0)
    normal_hours = min(work_hours, 8)

    normal_pay = normal_hours * hourly_wage

    # 加班費依勞基法比例
    overtime_pay = 0
    if overtime > 0:
        first2 = min(overtime, 2)
        overtime_pay += first2 * hourly_wage * 1.34
        if overtime > 2:
            overtime_pay += (overtime - 2) * hourly_wage * 1.67

    # 星期六 2倍日薪
    if datetime.now().weekday() == 5:
        normal_pay *= 2
        overtime_pay *= 2

    travel_fee = TRAVEL_FEES.get(data["location"], 0)
    income = normal_pay + overtime_pay + travel_fee

    return {"work_hours": round(work_hours, 1),
            "overtime": round(overtime, 1),
            "overtime_pay": round(overtime_pay, 0),
            "travel_fee": travel_fee,
            "income": round(income, 0)}

# ===== 寫入 Sheet =====
def append_to_sheet(user_id, date_str, data, calc):
    sheet = get_sheet()
    sheet.append_row([
        user_id, date_str, data.get("status",""), data.get("location",""),
        calc.get("work_hours",0), calc.get("overtime",0),
        calc.get("overtime_pay",0), calc.get("travel_fee",0),
        calc.get("income",0)
    ])

# ===== 月報表 =====
def generate_monthly_report(user_id):
    sheet = get_sheet()
    records = sheet.get_all_records()
    total_salary = BASE_SALARY + NEWBIE_ALLOWANCE + RENT_ALLOWANCE
    daily_base = total_salary / 22

    # 只抓自己
    records = [r for r in records if r["user_id"] == user_id]
    record_map = {r["日期"]: r for r in records}

    now = datetime.now()
    start = datetime(now.year, now.month, 1)
    end = (start.replace(month=start.month % 12 + 1, day=1)
           if start.month < 12 else datetime(start.year+1,1,1))

    current = start
    total_income = 0
    result = []
    worked_days = 0

    while current < end:
        date_str = current.strftime("%Y-%m-%d")
        if date_str in record_map:
            row = record_map[date_str]
            income = float(row["收入"])
            if row["狀態"] != "請假":
                worked_days += 1
        else:
            income = daily_base  # 自動補假日
        total_income += income
        result.append(f"{date_str}：{int(income)} 元")
        current += timedelta(days=1)

    # 全勤獎金只加一次
    if worked_days == 22 and all(r["狀態"] != "請假" for r in records):
        total_income += FULL_ATTENDANCE_BONUS

    return result, int(total_income)

# ===== Webhook =====
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

# ===== LINE 訊息 =====
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = event.message.text
    user_id = event.source.user_id

    # 查本月
    if text == "本月":
        report, total = generate_monthly_report(user_id)
        msg = "📅 本月薪資\n" + "\n".join(report)
        msg += f"\n\n💰 總收入：{total} 元"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))
        return

    data = parse_message(text)
    if data["status"] == "錯誤":
        reply = "格式錯誤：台南 800~2000 或 請假"
    else:
        date_str = datetime.now().strftime("%Y-%m-%d")
        calc = calculate_work(data)
        append_to_sheet(user_id, date_str, data, calc)
        reply = (f"📍 {data.get('location','')}\n"
                 f"工時：{calc['work_hours']} 小時\n"
                 f"加班：{calc['overtime']} 小時\n"
                 f"加班費：{calc['overtime_pay']} 元\n"
                 f"出差費：{calc['travel_fee']} 元\n"
                 f"今日收入：{calc['income']} 元")

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

# ===== 啟動 =====
if __name__ == "__main__":
    app.run()