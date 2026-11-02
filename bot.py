import os, json, csv, time, datetime
import requests
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
BASE = "https://openapivts.koreainvestment.com:29443"  # 모의투자 서버
APP_KEY = os.environ["KIS_APP_KEY"]
APP_SECRET = os.environ["KIS_APP_SECRET"]
TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHAT = os.environ["TELEGRAM_CHAT_ID"]
STATE_DIR = ".state"
os.makedirs(STATE_DIR, exist_ok=True)


def tg(msg):
    requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        data={"chat_id": TG_CHAT, "text": msg},
        timeout=10,
    )


def get_token():
    # 토큰은 하루 1번만 발급하고 재사용
    path = f"{STATE_DIR}/token.json"
    now = datetime.datetime.now(KST)
    if os.path.exists(path):
        t = json.load(open(path))
        if datetime.datetime.fromisoformat(t["expires"]) > now + datetime.timedelta(minutes=30):
            return t["token"]
    r = requests.post(
        f"{BASE}/oauth2/tokenP",
        json={"grant_type": "client_credentials", "appkey": APP_KEY, "appsecret": APP_SECRET},
        timeout=10,
    )
    r.raise_for_status()
    d = r.json()
    exp = now + datetime.timedelta(seconds=int(d.get("expires_in", 86400)))
    json.dump({"token": d["access_token"], "expires": exp.isoformat()}, open(path, "w"))
    return d["access_token"]


def headers(token, tr_id):
    return {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": tr_id,
        "custtype": "P",
    }


def stock_price(token, code):
    r = requests.get(
        f"{BASE}/uapi/domestic-stock/v1/quotations/inquire-price",
        headers=headers(token, "FHKST01010100"),
        params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code},
        timeout=10,
    )
    r.raise_for_status()
    o = r.json()["output"]
    return {
        "price": int(o["stck_prpr"]),
        "change_pct": float(o["prdy_ctrt"]),
        "volume": int(o["acml_vol"]),
    }


def index_price(token, code):
    try:
        r = requests.get(
            f"{BASE}/uapi/domestic-stock/v1/quotations/inquire-index-price",
            headers=headers(token, "FHPUP02100000"),
            params={"FID_COND_MRKT_DIV_CODE": "U", "FID_INPUT_ISCD": code},
            timeout=10,
        )
        r.raise_for_status()
        o = r.json()["output"]
        return {"price": float(o["bstp_nmix_prpr"]), "change_pct": float(o["bstp_nmix_prdy_ctrt"])}
    except Exception:
        return None  # 모의서버에서 지수 조회가 안 되면 건너뜀


def main():
    mode = os.environ.get("MODE", "check")
    if mode == "message":  # 텍스트를 그대로 텔레그램으로 전송
        tg(os.environ.get("MESSAGE", "").replace("\\n", "\n"))
        return
    now = datetime.datetime.now(KST)
    today = now.strftime("%Y-%m-%d")
    hhmm = now.strftime("%H:%M")
    cfg = json.load(open("watchlist.json", encoding="utf-8"))
    token = get_token()

    alert_path = f"{STATE_DIR}/alerts_{today}.json"
    sent = json.load(open(alert_path)) if os.path.exists(alert_path) else []
    rows, msgs = [], []

    for idx in cfg.get("indices", []):
        p = index_price(token, idx["code"])
        time.sleep(0.6)
        if p:
            rows.append([hhmm, idx["name"], p["price"], p["change_pct"], ""])

    for s in cfg["stocks"]:
        p = stock_price(token, s["code"])
        time.sleep(0.6)
        rows.append([hhmm, s["name"], p["price"], p["change_pct"], p["volume"]])

        checks = []
        tag = f"[{s.get('type', '보유')}] "
        if s.get("stop") and p["price"] <= s["stop"]:
            checks.append(("stop", f"🚨 {s['name']} 손절선 {s['stop']:,}원 이탈!\n현재 {p['price']:,}원 ({p['change_pct']:+.2f}%)"))
        lo, hi = s.get("entry_low"), s.get("entry_high")
        if lo and hi and lo <= p["price"] <= hi:
            checks.append(("entry", f"🟢 {s['name']} 진입 구간 도달 ({lo:,}~{hi:,}원)\n현재 {p['price']:,}원 ({p['change_pct']:+.2f}%) · 손절 {s.get('stop') or 0:,} / 목표 {s.get('target') or 0:,}"))
        if s.get("target") and p["price"] >= s["target"]:
            checks.append(("target", f"🎯 {s['name']} 목표가 {s['target']:,}원 도달!\n현재 {p['price']:,}원 ({p['change_pct']:+.2f}%)"))
        lim = cfg.get("move_alert_pct", 5)
        if abs(p["change_pct"]) >= lim:
            word = "급등" if p["change_pct"] > 0 else "급락"
            checks.append(("move", f"⚡ {s['name']} {word} {p['change_pct']:+.2f}%\n현재 {p['price']:,}원"))

        for key, msg in checks:
            k = f"{s['code']}_{key}"
            if k not in sent:  # 같은 알림은 하루 한 번만
                msgs.append(tag + msg)
                sent.append(k)

    json.dump(sent, open(alert_path, "w"))

    os.makedirs("data", exist_ok=True)
    path = f"data/{today}.csv"
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time", "name", "price", "change_pct", "volume"])
        w.writerows(rows)

    if mode in ("summary", "test"):
        title = "✅ 연결 테스트 성공" if mode == "test" else f"📊 {today} 장 마감 요약"
        lines = [title] + [f"- {r[1]}: {r[2]:,} ({r[3]:+.2f}%)" for r in rows]
        msgs.append("\n".join(lines))

    for m in msgs:
        tg(m)

    # 보낸 알림을 기록해서 Claude 체크인 때 읽을 수 있게 함
    if msgs and mode == "check":
        apath = f"data/alerts_{today}.csv"
        anew = not os.path.exists(apath)
        with open(apath, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if anew:
                w.writerow(["time", "message"])
            for m in msgs:
                w.writerow([hhmm, m.replace("\n", " / ")])


if __name__ == "__main__":
    main()
