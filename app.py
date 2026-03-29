from flask import Flask, request
import requests
import re

app = Flask(__name__)

# ===== LINE 設定 =====
CHANNEL_ACCESS_TOKEN = "SJ5NhauPKZwH3qXGpVGFEOkwBH7TKF5AG5M3OaGzKN2KCUGJ8snWPXHY/VuqheDKRh2gKxSFH357SBrri50RfjcRYX03pEEXFqtkD3hgW+zzUZ1M4zBf0A+f8olVLTaxamDyzvVjk9jXSua4auZhPQdB04t89/1O/w1cDnyilFU="

# ===== 薪資設定（依你目前）=====
BASE_SALARY = 45000  # 底薪+補助
HOURLY = BASE_SALARY / 30 / 8

travel_fee_map = {
    "台中": 200,
    "台南": 400,
    "高雄": 500
}

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
def calc_ot_pay(overtime):
    first2 = min(2, overtime)
    rest = max(0, overtime - 2)

    return first2 * HOURLY * 1.34 + rest * HOURLY * 1.67

# ===== LINE Webhook =====
@app.route("/callback", methods=['POST'])
def callback():
    data = request.json

    try:
        event = data['events'][0]
        text = event['message']['text']
        reply_token = event['replyToken']
    except:
        return "OK"

    result = parse_input(text)

    if not result:
        reply = "格式錯誤，例如：台南 800~2000"
    else:
        location, work_hours, overtime = result

        ot_pay = calc_ot_pay(overtime)
        travel_fee = travel_fee_map.get(location, 0)

        total = ot_pay + travel_fee

        reply = f"""📍 {location}
工時：{work_hours:.1f} 小時
加班：{overtime:.1f} 小時
加班費：{int(ot_pay)} 元
出差費：{travel_fee} 元
今日收入：{int(total)} 元"""

    reply_message(reply_token, reply)
    return "OK"

# ===== 啟動 =====
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
