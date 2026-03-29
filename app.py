from flask import Flask, request
import requests
import re
import datetime
import json
import os

app = Flask(__name__)

# ===== LINE 設定 =====
CHANNEL_ACCESS_TOKEN = "SJ5NhauPKZwH3qXGpVGFEOkwBH7TKF5AG5M3OaGzKN2KCUGJ8snWPXHY/VuqheDKRh2gKxSFH357SBrri50RfjcRYX03pEEXFqtkD3hgW+zzUZ1M4zBf0A+f8olVLTaxamDyzvVjk9jXSua4auZhPQdB04t89/1O/w1cDnyilFU="

# ===== 固定薪資 =====
BASE_SALARY = 28000      # 底薪
NEW_EMP_BONUS = 12000    # 新人補助
FULL_ATTENDANCE = 2000   # 全勤
RENT_ALLOWANCE = 3000    # 租屋補助

# ===== 時薪計算 =====
def calc_hourly(month_salary):
    return month_salary / 30 / 8

HOURLY = calc_hourly(BASE_SALARY + FULL_ATTENDANCE)  # 時薪基準

# ===== 出差費 =====
travel_fee_map = {
    "台中": 200,
    "台南": 400,
    "高雄": 500
}

# ===== 儲存月累計 =====
DATA_FILE = "monthly_data.json"

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"month": datetime.datetime.now().month, "total_income": 0}

def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)

# ===== LINE 回覆函式 =====
def reply_message(reply_token, text):
    url = "https://api.line.me/v2/bot/message/reply"
    headers = {
        "Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    body = {
        "replyToken": reply_token,
        "messages": [{"type": "text", "text": text}]
    }
    requests.post(url, headers=headers, json=body)

# ===== 解析輸入 =====
def parse_input(text):
    location = None
    for loc in travel_fee_map.keys():
        if loc in text:
            location = loc

    match = re.search(r'(\d{3,4})[~～-](\d{3,4})', text)
    if not match:
        return None

    start, end = match.groups()

    def to_time(t):
        if len(t) == 3:
            return int(t[0]), int(t[1:])
        return int(t[:2]), int(t[2:])

    sh, sm = to_time(start)
    eh, em = to_time(end)

    work_hours = (eh + em/60) - (sh + sm/60) - 1  # 扣1小時休息
    overtime = max(0, work_hours - 8)

    return location, work_hours, overtime

# ===== 計算加班費 =====
def calc_ot_pay(overtime, weekday):
    # 平日加班倍率
    first2 = min(2, overtime)
    rest = max(0, overtime - 2)
    ot = first2 * HOURLY * 1.34 + rest * HOURLY * 1.67

    # 星期六上班 2 倍
    if weekday == 5:  # Python weekday: 5 = Saturday
        ot = work_hours * HOURLY * 2

    return ot

# ===== Flask Webhook =====
@app.route("/callback", methods=['POST'])
def callback():
    data = request.json
    try:
        event = data['events'][0]
        text = event['message']['text']
        reply_token = event['replyToken']
    except:
        return "OK"

    # ===== 解析輸入 =====
    result = parse_input(text)
    today = datetime.datetime.now()
    weekday = today.weekday()  # 0 = Monday, 5 = Saturday

    if not result:
        reply = "格式錯誤，例如：台南 800~2000"
    else:
        location, work_hours, overtime = result

        # 星期六上班 2倍處理
        if weekday == 5:  # Saturday
            ot_pay = work_hours * HOURLY * 2
        else:
            ot_pay = calc_ot_pay(overtime, weekday)

        travel_fee = travel_fee_map.get(location, 0)
        total_today = ot_pay + travel_fee

        # ===== 更新月累計 =====
        data_month = load_data()
        if data_month["month"] != today.month:
            data_month = {"month": today.month, "total_income": 0}

        data_month["total_income"] += total_today
        save_data(data_month)

        # ===== 計算完整當月收入 =====
        full_month_income = BASE_SALARY + NEW_EMP_BONUS + FULL_ATTENDANCE + RENT_ALLOWANCE + data_month["total_income"]

        reply = f"""📍 {location}
工時：{work_hours:.1f} 小時
加班：{overtime:.1f} 小時
加班費：{int(ot_pay)} 元
出差費：{travel_fee} 元
今日收入：{int(total_today)} 元
📅 本月累計收入（含底薪+補助+今日累計）：{int(full_month_income)} 元"""

    reply_message(reply_token, reply)
    return "OK"

# ===== 首頁避免 404 =====
@app.route("/")
def home():
    return "Bot is running"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
