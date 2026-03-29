from flask import Flask, request
import requests, re, datetime, json, os
import csv
from io import StringIO

app = Flask(__name__)

# ===== LINE 設定 =====
CHANNEL_ACCESS_TOKEN = "SJ5NhauPKZwH3qXGpVGFEOkwBH7TKF5AG5M3OaGzKN2KCUGJ8snWPXHY/VuqheDKRh2gKxSFH357SBrri50RfjcRYX03pEEXFqtkD3hgW+zzUZ1M4zBf0A+f8olVLTaxamDyzvVjk9jXSua4auZhPQdB04t89/1O/w1cDnyilFU="

# ===== 固定薪資 =====
BASE_SALARY = 28000
NEW_EMP_BONUS = 12000
FULL_ATTENDANCE = 2000
RENT_ALLOWANCE = 3000

# ===== 時薪 =====
def calc_hourly(month_salary):
    return month_salary / 30 / 8
HOURLY = calc_hourly(BASE_SALARY + FULL_ATTENDANCE)

# ===== 出差費 =====
travel_fee_map = {"台中":200,"台南":400,"高雄":500}

# ===== 儲存資料 =====
DATA_FILE = "monthly_data.json"

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ===== LINE 回覆 =====
def reply_message(reply_token, text):
    url = "https://api.line.me/v2/bot/message/reply"
    headers = {
        "Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    body = {"replyToken": reply_token, "messages":[{"type":"text","text":text}]}
    requests.post(url, headers=headers, json=body)

# ===== 解析工作輸入 =====
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
        if len(t)==3: return int(t[0]), int(t[1:])
        return int(t[:2]), int(t[2:])
    sh, sm = to_time(start)
    eh, em = to_time(end)
    work_hours = (eh + em/60) - (sh + sm/60) - 1  # 扣休息
    overtime = max(0, work_hours - 8)
    return location, work_hours, overtime

# ===== 加班費計算 =====
def calc_ot_pay(work_hours, overtime, weekday):
    if weekday == 5:  # Saturday
        return work_hours * HOURLY * 2
    # 平日加班
    first2 = min(2, overtime)
    rest = max(0, overtime-2)
    return first2*HOURLY*1.34 + rest*HOURLY*1.67

# ===== 生成 CSV =====
def generate_csv(records):
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["日期","星期","地點","工時","加班","加班費","出差費","今日收入"])
    total_ot = total_travel = total_income = 0
    for r in records:
        writer.writerow([
            r['date'], r['weekday'], r['location'],
            r['work_hours'], r['overtime'],
            int(r['ot_pay']), r['travel_fee'], int(r['today_income'])
        ])
        total_ot += r['ot_pay']
        total_travel += r['travel_fee']
        total_income += r['today_income']
    writer.writerow(["合計","","","","", int(total_ot), int(total_travel), int(total_income)])
    return output.getvalue()

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

    today = datetime.datetime.now()
    month_key = today.strftime("%Y-%m")
    weekday = today.weekday()
    all_data = load_data()
    if month_key not in all_data:
        all_data[month_key] = {"records":[]}

    # ===== 查本月表格 =====
    if text in ["本月表格", "查本月"]:
        records = all_data[month_key]["records"]
        if not records:
            reply_message(reply_token, "本月尚無紀錄")
            return "OK"
        csv_text = generate_csv(records)
        full_month_total = sum(r['today_income'] for r in records)
        full_income = BASE_SALARY + NEW_EMP_BONUS + FULL_ATTENDANCE + RENT_ALLOWANCE + full_month_total
        reply = f"📅 {month_key} 月加班表（CSV 格式）\n{csv_text}\n本月累計收入（含底薪+補助）：{int(full_income)}"
        reply_message(reply_token, reply)
        return "OK"

    # ===== 查歷史月份 =====
    match_month = re.match(r"查月份 (\d{4}-\d{2})", text)
    if match_month:
        key = match_month.group(1)
        if key not in all_data or not all_data[key]["records"]:
            reply_message(reply_token, f"{key} 尚無加班紀錄")
            return "OK"
        csv_text = generate_csv(all_data[key]["records"])
        full_month_total = sum(r['today_income'] for r in all_data[key]["records"])
        full_income = BASE_SALARY + NEW_EMP_BONUS + FULL_ATTENDANCE + RENT_ALLOWANCE + full_month_total
        reply = f"📅 {key} 月加班表（CSV 格式）\n{csv_text}\n累計收入（含底薪+補助）：{int(full_income)}"
        reply_message(reply_token, reply)
        return "OK"

    # ===== 解析日常工作輸入 =====
    result = parse_input(text)
    if not result:
        reply_message(reply_token, "格式錯誤，例如：台南 800~2000")
        return "OK"
    location, work_hours, overtime = result
    ot_pay = calc_ot_pay(work_hours, overtime, weekday)
    travel_fee = travel_fee_map.get(location, 0)
    today_income = ot_pay + travel_fee

    # ===== 存資料 =====
    record = {
        "date": today.strftime("%Y-%m-%d"),
        "weekday": today.strftime("%A"),
        "location": location,
        "work_hours": work_hours,
        "overtime": overtime,
        "ot_pay": ot_pay,
        "travel_fee": travel_fee,
        "today_income": today_income
    }
    all_data[month_key]["records"].append(record)
    save_data(all_data)

    # ===== 回覆訊息 =====
    full_month_total = sum(r['today_income'] for r in all_data[month_key]["records"])
    full_income = BASE_SALARY + NEW_EMP_BONUS + FULL_ATTENDANCE + RENT_ALLOWANCE + full_month_total
    reply = f"""📍 {location}
工時：{work_hours:.1f} 小時
加班：{overtime:.1f} 小時
加班費：{int(ot_pay)} 元
出差費：{travel_fee} 元
今日收入：{int(today_income)} 元
📅 本月累計收入（含底薪+補助+今日累計）：{int(full_income)} 元"""
    reply_message(reply_token, reply)
    return "OK"

@app.route("/")
def home():
    return "Bot is running"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
