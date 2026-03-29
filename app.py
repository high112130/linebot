from flask import Flask, request
import requests, re, datetime, json, os
import csv
from io import StringIO

app = Flask(__name__)

CHANNEL_ACCESS_TOKEN = "SJ5NhauPKZwH3qXGpVGFEOkwBH7TKF5AG5M3OaGzKN2KCUGJ8snWPXHY/VuqheDKRh2gKxSFH357SBrri50RfjcRYX03pEEXFqtkD3hgW+zzUZ1M4zBf0A+f8olVLTaxamDyzvVjk9jXSua4auZhPQdB04t89/1O/w1cDnyilFU="

# ===== 固定薪資 =====
BASE_SALARY = 28000
NEW_EMP_BONUS = 12000
FULL_ATTENDANCE = 2000
RENT_ALLOWANCE = 3000

HOURLY = (BASE_SALARY + FULL_ATTENDANCE) / 30 / 8

travel_fee_map = {"台中":200,"台南":400,"高雄":500}

DATA_FILE = "monthly_data.json"

# ===== 資料讀寫 =====
def load_data():
    if os.path.exists(DATA_FILE):
        return json.load(open(DATA_FILE, "r", encoding="utf-8"))
    return {}

def save_data(data):
    json.dump(data, open(DATA_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

# ===== LINE 回覆 =====
def reply(reply_token, text):
    requests.post(
        "https://api.line.me/v2/bot/message/reply",
        headers={"Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}"},
        json={"replyToken": reply_token, "messages":[{"type":"text","text":text}]}
    )

# ===== 時間解析 =====
def parse_time(text):
    match = re.search(r'(\d{3,4})[~～-](\d{3,4})', text)
    if not match:
        return None
    s,e = match.groups()

    def t(x):
        return (int(x[0]), int(x[1:])) if len(x)==3 else (int(x[:2]), int(x[2:]))

    sh,sm = t(s)
    eh,em = t(e)

    work = (eh+em/60)-(sh+sm/60)-1

    if work <= 0 or work > 24:
        return None

    ot = max(0, work-8)
    return work, ot

# ===== 加班費 =====
def calc_ot(work, ot, weekday):
    if weekday == 5:
        return work * HOURLY * 2
    return min(2,ot)*HOURLY*1.34 + max(0,ot-2)*HOURLY*1.67

# ===== CSV =====
def gen_csv(records):
    out = StringIO()
    w = csv.writer(out)
    w.writerow(["日期","狀態","地點","工時","加班","加班費","出差費","收入"])
    total=0
    for r in records:
        w.writerow([r['date'],r['status'],r.get('location',''),r.get('work',0),
                    r.get('ot',0),int(r.get('ot_pay',0)),r.get('travel',0),int(r.get('income',0))])
        total+=r.get('income',0)
    w.writerow(["合計","","","","","", "", int(total)])
    return out.getvalue(), total

# ===== 核心 =====
@app.route("/callback", methods=['POST'])
def callback():
    data = request.json
    try:
        event = data['events'][0]
        text = event['message']['text']
        token = event['replyToken']
    except:
        return "OK"

    today = datetime.datetime.now()
    key = today.strftime("%Y-%m")
    date = today.strftime("%Y-%m-%d")
    weekday = today.weekday()

    db = load_data()
    if key not in db:
        db[key] = {"records":[]}

    records = db[key]["records"]

    # ===== 查詢 =====
    if "本月" in text:
        if not records:
            reply(token,"本月無資料")
            return "OK"

        csv_text,total = gen_csv(records)

        # 全勤判斷
        has_leave = any(r['status']=="請假" for r in records)
        full_att = 0 if has_leave else FULL_ATTENDANCE

        final = BASE_SALARY + NEW_EMP_BONUS + RENT_ALLOWANCE + full_att + total

        reply(token,f"📅 {key}\n{csv_text}\n總收入:{int(final)}")
        return "OK"

    # ===== 查月份 =====
    m = re.match(r"查月份 (\d{4}-\d{2})", text)
    if m:
        k = m.group(1)
        if k not in db:
            reply(token,"無資料")
            return "OK"
        csv_text,total = gen_csv(db[k]["records"])
        reply(token,f"{k}\n{csv_text}")
        return "OK"

    # ===== 請假 =====
    if "請假" in text or "休假" in text:
        # 覆蓋當天
        records = [r for r in records if r['date'] != date]

        status = "請假"
        if "半天" in text:
            income = -BASE_SALARY/30/2
        else:
            income = -BASE_SALARY/30

        records.append({
            "date":date,
            "status":status,
            "income":income
        })

        db[key]["records"]=records
        save_data(db)

        reply(token,"已記錄請假")
        return "OK"

    # ===== 工作 =====
    loc = next((l for l in travel_fee_map if l in text), None)
    if not loc:
        reply(token,"❌ 請輸入地點（台中/台南/高雄）")
        return "OK"

    parsed = parse_time(text)
    if not parsed:
        reply(token,"❌ 時間錯誤，例如：台南 800~2000")
        return "OK"

    work, ot = parsed
    ot_pay = calc_ot(work, ot, weekday)
    travel = travel_fee_map[loc]
    income = ot_pay + travel

    # 覆蓋同一天
    records = [r for r in records if r['date'] != date]

    records.append({
        "date":date,
        "status":"上班",
        "location":loc,
        "work":work,
        "ot":ot,
        "ot_pay":ot_pay,
        "travel":travel,
        "income":income
    })

    db[key]["records"]=records
    save_data(db)

    reply(token,f"""📍{loc}
工時:{work:.1f}
加班:{ot:.1f}
加班費:{int(ot_pay)}
出差:{travel}
收入:{int(income)}""")

    return "OK"

@app.route("/")
def home():
    return "OK"
