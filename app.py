# app.py
import os
import json
from datetime import datetime
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import csv

# ===== LINE Bot 設定 =====
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# ===== Google Sheet 設定 =====
SHEET_ID = os.environ.get("SHEET_ID")
scope = ["https://spreadsheets.google.com/feeds",
         "https://www.googleapis.com/auth/drive"]
creds_json = json.loads(os.environ.get("GOOGLE_CREDENTIALS"))
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_json, scope)
client = gspread.authorize(creds)
sheet = client.open_by_key(SHEET_ID).sheet1

# ===== Bot 記錄設定 =====
BASE_SALARY = 28000
NEWBIE_ALLOWANCE = 12000
FULL_ATTENDANCE_BONUS = 2000
RENT_ALLOWANCE = 3000
OVERTIME_RATE = 100  # 每小時加班費基準
TRAVEL_FEES = {"台中":200, "高雄":500, "台南":400}

# ===== Flask App =====
app = Flask(__name__)

# ===== 工時資料格式 =====
# Sheet 欄位: 日期, 狀態, 地點, 工時, 加班, 加班費, 出差費, 收入

def parse_message(text):
    """
    範例訊息：
    '台南 800~2000' -> 地點, 上班時間, 下班時間
    '請假' -> 狀態請假
    """
    text = text.strip()
    if text.startswith("請假"):
        return {"status":"請假"}
    try:
        parts = text.split()
        location = parts[0]
        time_range = parts[1].split("~")
        start = float(time_range[0])/100
        end = float(time_range[1])/100
        return {"status":"上班", "location":location, "start":start, "end":end}
    except Exception:
        return {"status":"錯誤"}

def calculate_work(data):
    """計算工時、加班、出差費、今日收入"""
    if data["status"] == "請假":
        return {
            "work_hours":0, "overtime":0, "overtime_pay":0,
            "travel_fee":0, "income": -BASE_SALARY/22  # 假設月薪平均每日
        }

    start = data["start"]
    end = data["end"]
    work_hours = end - start - 1  # 扣掉午休1小時
    work_hours = max(work_hours, 0)
    weekday = datetime.now().weekday()  # 0=Monday
    overtime = max(work_hours - 8, 0)
    overtime_pay = 0

    # 週六雙倍薪水
    if weekday == 5:
        base_pay = BASE_SALARY/22*2
        overtime_pay = overtime*OVERTIME_RATE*2
    else:
        base_pay = BASE_SALARY/22
        overtime_pay = overtime*OVERTIME_RATE

    travel_fee = TRAVEL_FEES.get(data["location"],0)
    income = base_pay + overtime_pay + travel_fee

    return {
        "work_hours":round(work_hours,1),
        "overtime":round(overtime,1),
        "overtime_pay":round(overtime_pay,0),
        "travel_fee":travel_fee,
        "income":round(income,0)
    }

def append_to_sheet(date_str, data, calc):
    """將資料寫入 Google Sheet"""
    sheet.append_row([
        date_str,
        data.get("status",""),
        data.get("location",""),
        calc.get("work_hours",0),
        calc.get("overtime",0),
        calc.get("overtime_pay",0),
        calc.get("travel_fee",0),
        calc.get("income",0)
    ])

def generate_monthly_csv():
    """產生本月 CSV"""
    records = sheet.get_all_records()
    now = datetime.now()
    month_str = now.strftime("%Y-%m")
    filename = f"{month_str}_salary.csv"
    with open(filename,"w",newline="",encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["日期","狀態","地點","工時","加班","加班費","出差費","收入"])
        for row in records:
            if row["日期"].startswith(month_str):
                writer.writerow([row[k] for k in ["日期","狀態","地點","工時","加班","加班費","出差費","收入"]])
    return filename

# ===== Flask 路由 =====
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

# ===== LINE 訊息處理 =====
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = event.message.text
    data = parse_message(text)
    if data["status"] == "錯誤":
        reply = "訊息格式錯誤，請輸入：地點 800~2000 或 請假"
    else:
        date_str = datetime.now().strftime("%Y-%m-%d")
        calc = calculate_work(data)
        append_to_sheet(date_str, data, calc)
        reply = (f"📍 {data.get('location','')} \n"
                 f"工時：{calc['work_hours']} 小時\n"
                 f"加班：{calc['overtime']} 小時\n"
                 f"加班費：{calc['overtime_pay']} 元\n"
                 f"出差費：{calc['travel_fee']} 元\n"
                 f"今日收入：{calc['income']} 元")

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply)
    )

# ===== 啟動 Flask =====
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
