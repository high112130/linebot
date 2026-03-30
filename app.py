import os
import json
import re
from datetime import datetime
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# ===== LINE Bot =====
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# ===== Google Sheet =====
SHEET_ID = os.environ.get("SHEET_ID")
scope = ["https://spreadsheets.google.com/feeds",
         "https://www.googleapis.com/auth/drive"]

creds_json = json.loads(os.environ.get("GOOGLE_CREDENTIALS"))
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_json, scope)
client = gspread.authorize(creds)
sheet = client.open_by_key(SHEET_ID).sheet1

# ===== 薪資設定 =====
BASE = 28000
NEWBIE = 12000
RENT = 3000
FULL_ATTENDANCE = 2000

TOTAL_SALARY = BASE + NEWBIE + RENT
HOURLY = TOTAL_SALARY / 30 / 8

TRAVEL_FEES = {"台中":200, "高雄":500, "台南":400}

app = Flask(__name__)

# ===== 訊息解析 =====
def parse_message(text):
    text = text.strip()

    if "請假" in text:
        return {"status": "請假"}

    text = text.replace("～", "~").replace("-", "~")

    match = re.search(r"(\D+)\s*(\d{3,4})~(\d{3,4})", text)
    if not match:
        return {"status": "錯誤"}

    return {
        "status": "上班",
        "location": match.group(1).strip(),
        "start": float(match.group(2))/100,
        "end": float(match.group(3))/100
    }

# ===== 計算 =====
def calculate_work(data):
    today = datetime.now()
    weekday = today.weekday()

    if data["status"] == "請假":
        return {
            "work_hours":0,
            "overtime":0,
            "overtime_pay":0,
            "travel_fee":0,
            "income":0,
            "leave":True
        }

    start = data["start"]
    end = data["end"]

    work_hours = max(end - start - 1, 0)

    normal_hours = min(work_hours, 8)
    overtime = max(work_hours - 8, 0)

    # 正常薪資
    normal_pay = normal_hours * HOURLY

    # 加班費（勞基法）
    overtime_pay = 0
    if overtime > 0:
        first2 = min(overtime, 2)
        overtime_pay += first2 * HOURLY * 1.34

        if overtime > 2:
            overtime_pay += (overtime - 2) * HOURLY * 1.67

    # 星期六 2倍
    if weekday == 5:
        normal_pay *= 2
        overtime_pay *= 2

    travel_fee = TRAVEL_FEES.get(data["location"], 0)

    income = normal_pay + overtime_pay + travel_fee

    return {
        "work_hours":round(work_hours,1),
        "overtime":round(overtime,1),
        "overtime_pay":round(overtime_pay,0),
        "travel_fee":travel_fee,
        "income":round(income,0),
        "leave":False
    }

# ===== 寫入 Sheet =====
def append_to_sheet(date_str, data, calc):
    sheet.append_row([
        date_str,
        data.get("status",""),
        data.get("location",""),
        calc.get("work_hours",0),
        calc.get("overtime",0),
        calc.get("overtime_pay",0),
        calc.get("travel_fee",0),
        calc.get("income",0),
        calc.get("leave",False)
    ])

# ===== 月統計 =====
def monthly_summary():
    records = sheet.get_all_records()
    now = datetime.now()
    month = now.strftime("%Y-%m")

    total_income = 0
    total_hours = 0
    total_ot = 0
    has_leave = False

    for r in records:
        if r["日期"].startswith(month):
            total_income += float(r["收入"])
            total_hours += float(r["工時"])
            total_ot += float(r["加班"])
            if str(r.get("是否請假")).lower() == "true":
                has_leave = True

    bonus = 0 if has_leave else FULL_ATTENDANCE
    total_income += bonus

    return f"""📅 本月統計
總工時：{round(total_hours,1)} 小時
總加班：{round(total_ot,1)} 小時
全勤：{"❌ 無" if has_leave else "✅ 有"}
全勤獎金：{bonus} 元
💰 總收入：{round(total_income,0)} 元
"""

# ===== Webhook =====
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return 'OK'

# ===== LINE 處理 =====
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = event.message.text

    # 👉 查本月
    if text == "本月":
        reply = monthly_summary()
    else:
        data = parse_message(text)

        if data["status"] == "錯誤":
            reply = "格式錯誤：台中 800~2000 或 請假"
        else:
            date_str = datetime.now().strftime("%Y-%m-%d")
            calc = calculate_work(data)
            append_to_sheet(date_str, data, calc)

            reply = (f"📍 {data.get('location','')}\n"
                     f"工時：{calc['work_hours']} 小時\n"
                     f"加班：{calc['overtime']} 小時\n"
                     f"加班費：{calc['overtime_pay']} 元\n"
                     f"出差費：{calc['travel_fee']} 元\n"
                     f"今日收入：{calc['income']} 元")

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply)
    )

# ===== 啟動 =====
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))