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
        "open": int(o.get("stck_oprc") or 0),
        "high": int(o.get("stck_hgpr") or 0),
        "low": int(o.get("stck_lwpr") or 0),
    }


def score_stock(s, p):
    """관심 종목 하루 성적: 진입 구간 도달 여부와, 샀다면 종가 기준 손익"""
    lo, hi = s.get("entry_low"), s.get("entry_high")
    if not (lo and hi and p.get("low") and p.get("high")):
        return None
    touched = p["low"] <= hi and p["high"] >= lo
    fill = None
    pct = None
    if touched:
        # 시가가 구간 안이면 시가, 위에서 내려오면 구간 상단, 아래에서 올라오면 구간 하단에 체결됐다고 가정
        fill = min(hi, max(lo, p["open"])) if p["open"] else hi
        pct = round((p["price"] - fill) / fill * 100, 2)
    return {
        "touched": touched,
        "fill": fill,
        "close_pct": pct,
        "stop_hit": bool(s.get("stop")) and p["low"] <= s["stop"],
        "target_hit": bool(s.get("target")) and p["high"] >= s["target"],
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


def scorecard(cfg, quotes, today, kospi_pct=None):
    """오늘 관심 종목 채점 → data/scorecard.csv 누적 + 텔레그램 요약 문구"""
    os.makedirs("data", exist_ok=True)
    path = "data/scorecard.csv"
    new = not os.path.exists(path)
    lines = [f"📋 {today} 관심 종목 성적표" + (f" (코스피 {kospi_pct:+.2f}%)" if kospi_pct is not None else "")]
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["date", "name", "code", "entry_low", "entry_high", "stop", "target",
                        "open", "high", "low", "close", "touched", "fill", "close_pct", "stop_hit", "target_hit", "kospi_pct"])
        for s in cfg["stocks"]:
            if s.get("type") != "관심" or s["code"] not in quotes:
                continue
            p = quotes[s["code"]]
            r = score_stock(s, p)
            if r is None:
                continue
            w.writerow([today, s["name"], s["code"], s.get("entry_low"), s.get("entry_high"), s.get("stop"), s.get("target"),
                        p["open"], p["high"], p["low"], p["price"], r["touched"], r["fill"], r["close_pct"], r["stop_hit"], r["target_hit"], kospi_pct])
            if r["touched"]:
                extra = " · 손절선 터치" if r["stop_hit"] else (" · 목표가 터치" if r["target_hit"] else "")
                vs = f", 코스피 대비 {r['close_pct'] - kospi_pct:+.2f}%p" if kospi_pct is not None else ""
                lines.append(f"- {s['name']}: 진입 ✅ {r['fill']:,} → 종가 {p['price']:,} ({r['close_pct']:+.2f}%{vs}){extra}")
            else:
                side = "위" if p["low"] > s["entry_high"] else "아래"
                lines.append(f"- {s['name']}: 진입 ❌ (하루 내내 구간 {side}) 종가 {p['price']:,}")
    return "\n".join(lines)


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
    quotes = {}

    for idx in cfg.get("indices", []):
        p = index_price(token, idx["code"])
        time.sleep(0.6)
        if p:
            rows.append([hhmm, idx["name"], p["price"], p["change_pct"], ""])

    for s in cfg["stocks"]:
        p = stock_price(token, s["code"])
        quotes[s["code"]] = p
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
            # 기준가를 장중에 바꾸면 같은 날이라도 새 기준으로 다시 알림
            lv = {"entry": s.get("entry_low"), "stop": s.get("stop"), "target": s.get("target")}.get(key, "")
            k = f"{s['code']}_{key}_{lv}"
            if k not in sent:  # 같은 알림은 하루 한 번만
                note = (s.get("notes") or {}).get(key) or s.get("note")
                msgs.append(tag + msg + (f"\n💬 Claude: {note}" if note else ""))
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

    if mode == "summary":
        try:
            kospi = next((r[3] for r in rows if r[1] == "코스피"), None)
            msgs.append(scorecard(cfg, quotes, today, kospi))
        except Exception as e:
            msgs.append(f"📋 성적표 계산 실패: {e}")

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
