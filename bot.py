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
        # VWAP(당일 평균 체결가) = 누적 거래대금 / 누적 거래량
        "vwap": round(int(o.get("acml_tr_pbmn") or 0) / int(o["acml_vol"])) if int(o.get("acml_vol") or 0) else 0,
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
        return {"price": float(o["bstp_nmix_prpr"]), "change_pct": float(o["bstp_nmix_prdy_ctrt"]),
                "high": float(o.get("bstp_nmix_hgpr") or 0), "low": float(o.get("bstp_nmix_lwpr") or 0)}
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


DART_KEY = os.environ.get("DART_API_KEY", "")
DART_WORDS = ["단일판매", "공급계약", "잠정", "영업실적", "자기주식취득", "자기주식 취득", "유상증자", "무상증자",
              "최대주주변경", "최대주주 변경", "합병", "분할", "공개매수", "신규시설투자", "매출액또는손익구조",
              "전환사채", "수주", "기술이전", "품목허가", "임상"]


def daily_stats(token, code, days=45):
    """일봉으로 20일선·ATR(14) 계산. 반환: {close, ma20, above20, atr, atr_pct}"""
    end = datetime.datetime.now(KST)
    start = end - datetime.timedelta(days=days)
    r = requests.get(
        f"{BASE}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
        headers=headers(token, "FHKST03010100"),
        params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code,
                "FID_INPUT_DATE_1": start.strftime("%Y%m%d"), "FID_INPUT_DATE_2": end.strftime("%Y%m%d"),
                "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "0"},
        timeout=10,
    )
    r.raise_for_status()
    rows = [x for x in r.json().get("output2", []) if x.get("stck_clpr")]
    rows.sort(key=lambda x: x["stck_bsop_date"])  # 오래된 날 → 최근
    c = [int(x["stck_clpr"]) for x in rows]
    h = [int(x["stck_hgpr"]) for x in rows]
    l = [int(x["stck_lwpr"]) for x in rows]
    if len(c) < 21:
        return None
    ma20 = sum(c[-20:]) / 20
    trs = [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, len(c))]
    atr = sum(trs[-14:]) / 14
    return {"close": c[-1], "ma20": round(ma20), "above20": c[-1] > ma20,
            "ma20_up": ma20 > sum(c[-21:-1]) / 20, "atr": round(atr), "atr_pct": round(atr / c[-1] * 100, 2)}


def amount_rank(token, market="J", top=20):
    """거래대금 상위 종목 (KIS 거래량순위, 거래금액순). 모의서버에서 막히면 빈 목록"""
    try:
        r = requests.get(
            f"{BASE}/uapi/domestic-stock/v1/quotations/volume-rank",
            headers=headers(token, "FHPST01710000"),
            params={"FID_COND_MRKT_DIV_CODE": market, "FID_COND_SCR_DIV_CODE": "20171", "FID_INPUT_ISCD": "0000",
                    "FID_DIV_CLS_CODE": "1", "FID_BLNG_CLS_CODE": "3", "FID_TRGT_CLS_CODE": "111111111",
                    "FID_TRGT_EXLS_CLS_CODE": "0000000000", "FID_INPUT_PRICE_1": "", "FID_INPUT_PRICE_2": "",
                    "FID_VOL_CNT": "", "FID_INPUT_DATE_1": ""},
            timeout=10,
        )
        r.raise_for_status()
        js = r.json()
        out = js.get("output") or []
        with open("data/rank_debug.txt", "a", encoding="utf-8") as f:
            f.write(f"{datetime.datetime.now(KST):%m-%d %H:%M} {market} rt={js.get('rt_cd')} msg={js.get('msg1')} n={len(out)} first={out[:1]}\n")
        return [{"code": x["mksc_shrn_iscd"], "name": x["hts_kor_isnm"], "price": int(x["stck_prpr"]),
                 "chg": float(x["prdy_ctrt"]), "amount": int(x.get("acml_tr_pbmn") or 0)} for x in out[:top]]
    except Exception as e:
        print("amount_rank fail:", market, e)
        return []


def dart_issues():
    """어제·오늘 상장사 주요 공시 (DART). 반환: [{code,name,title}]"""
    if not DART_KEY:
        return []
    now = datetime.datetime.now(KST)
    bgn = (now - datetime.timedelta(days=3 if now.weekday() == 0 else 1)).strftime("%Y%m%d")
    out = []
    for page in (1, 2, 3):
        try:
            r = requests.get("https://opendart.fss.or.kr/api/list.json",
                             params={"crtfc_key": DART_KEY, "bgn_de": bgn, "end_de": now.strftime("%Y%m%d"),
                                     "page_no": page, "page_count": 100}, timeout=15)
            d = r.json()
        except Exception as e:
            print("dart fail:", e)
            break
        for x in d.get("list", []):
            if x.get("corp_cls") in ("Y", "K") and x.get("stock_code") and any(w in x["report_nm"] for w in DART_WORDS):
                if "기재정정" in x["report_nm"] or "첨부" in x["report_nm"]:
                    continue
                out.append({"code": x["stock_code"], "name": x["corp_name"], "title": x["report_nm"].strip()})
        if page >= int(d.get("total_page", 1)):
            break
    seen, uniq = set(), []
    for x in out:
        if x["code"] not in seen:
            seen.add(x["code"]); uniq.append(x)
    return uniq


def premarket(token, today):
    """08:45 이슈 종목 후보: 공시 + 거래대금 상위(NXT 프리마켓 → 없으면 전일 KRX) + 일봉 추세"""
    cand = {}
    for x in dart_issues()[:40]:
        cand.setdefault(x["code"], {"name": x["name"], "why": []})["why"].append("공시: " + x["title"])
    rank = amount_rank(token, "NX") or amount_rank(token, "J")
    for x in rank:
        if abs(x["chg"]) >= 3:
            cand.setdefault(x["code"], {"name": x["name"], "why": []})["why"].append(f"거래대금 상위 {x['chg']:+.1f}%")
    scored = []
    for code, v in list(cand.items())[:25]:
        try:
            st = daily_stats(token, code)
        except Exception as e:
            print("daily fail", code, e); st = None
        time.sleep(0.6)
        if not st:
            continue
        score = len(v["why"]) + (2 if st["above20"] and st["ma20_up"] else 1 if st["above20"] else 0)
        scored.append({"code": code, "name": v["name"], "why": " / ".join(v["why"]), **st, "score": score})
    scored.sort(key=lambda x: -x["score"])
    os.makedirs("data", exist_ok=True)
    with open(f"data/premarket_{today}.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["code", "name", "why", "close", "ma20", "above20", "ma20_up", "atr", "atr_pct", "score"])
        for x in scored:
            w.writerow([x["code"], x["name"], x["why"], x["close"], x["ma20"], x["above20"], x["ma20_up"], x["atr"], x["atr_pct"], x["score"]])
    picks = [x for x in scored if x["above20"]][:5]
    cfg = json.load(open("watchlist.json", encoding="utf-8"))
    cfg["stocks"] = [s for s in cfg["stocks"] if s.get("type") != "이슈"]
    have = {s["code"] for s in cfg["stocks"]}
    for x in picks:
        if x["code"] not in have:
            cfg["stocks"].append({"name": x["name"], "code": x["code"], "type": "이슈", "orb": True,
                                  "notes": {"orb_up": f"이슈 종목 시가 범위 돌파. 근거: {x['why'][:60]}. ATR {x['atr_pct']}%. 손절은 시가 범위 중간."}})
    json.dump(cfg, open("watchlist.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    lines = [f"🔎 {today} 오늘의 이슈 종목 후보 (08:45)"]
    for x in scored[:8]:
        trend = "20일선↑" if x["above20"] and x["ma20_up"] else ("20일선 위" if x["above20"] else "20일선 아래❌")
        lines.append(f"- {x['name']}({x['code']}) {trend} · ATR {x['atr_pct']}% · {x['why'][:50]}")
    if not scored:
        lines.append("- 후보 없음 (공시·거래대금 조회 결과 없음)")
    lines.append(f"👀 시가 범위 감시 추가: {', '.join(x['name'] for x in picks) or '없음'}")
    tg("\n".join(lines))
    print("\n".join(lines))


# ───────── 단타 보조: 호가 단위·진입 계획·포지션·텔레그램 명령 ─────────
ETF_PREFIX = ("KODEX", "TIGER", "ACE", "RISE", "SOL", "PLUS", "KOSEF", "HANARO", "KIWOOM", "1Q", "미래에셋")
POS_PATH = "positions.json"
TRADES_PATH = "data/trades.csv"
RISK_PER_TRADE = 10000
DAY_LIMIT, WEEK_LIMIT, MAX_ENTRIES = -20000, -50000, 2
# 인버스·레버리지 ETF는 지수 조건도 같이 본다: (지수코드, "below_low"|"above_mid")
INDEX_COND = {"114800": ("0001", "below_low"), "252670": ("0001", "below_low"),
              "122630": ("0001", "above_mid"), "233740": ("1001", "above_mid")}


def is_etf(name):
    return name.startswith(ETF_PREFIX)


def tick(price, etf=False):
    if etf:
        return 1 if price < 2000 else 5
    for lim, t in ((2000, 1), (5000, 5), (20000, 10), (50000, 50), (200000, 100), (500000, 500)):
        if price < lim:
            return t
    return 1000


def rnd(x, t, up=False):
    import math
    return int((math.ceil if up else math.floor)(x / t) * t)


def load_pos():
    if os.path.exists(POS_PATH):
        return json.load(open(POS_PATH, encoding="utf-8"))
    return {"cash": 1100000, "positions": {}}


def save_pos(d):
    json.dump(d, open(POS_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


def trail_for(entry, stop, etf):
    R = entry - stop
    t = tick(entry, etf)
    if R / entry < 0.015:  # 1R이 작은 종목은 +1.5R에서 본전 (플레이북 규칙 11)
        return [{"at": rnd(entry + 1.5 * R, t, True), "stop": entry}]
    return [{"at": rnd(entry + R, t, True), "stop": entry},
            {"at": rnd(entry + 1.5 * R, t, True), "stop": rnd(entry + 0.5 * R, t)}]


def minute_bars(token, code, hhmmss):
    """당일 1분봉 최근 30개 (오래된 것 → 최근 순서)"""
    r = requests.get(
        f"{BASE}/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice",
        headers=headers(token, "FHKST03010200"),
        params={"FID_ETC_CLS_CODE": "", "FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code,
                "FID_INPUT_HOUR_1": hhmmss, "FID_PW_DATA_INCU_YN": "Y"},
        timeout=10,
    )
    r.raise_for_status()
    out = r.json().get("output2") or []
    bars = [{"t": x["stck_cntg_hour"], "o": int(x["stck_oprc"]), "h": int(x["stck_hgpr"]),
             "l": int(x["stck_lwpr"]), "c": int(x["stck_prpr"]), "v": int(x.get("cntg_vol") or 0)} for x in out]
    return list(reversed(bars))


def rsi(closes, n=14):
    if len(closes) <= n:
        return None
    g = l = 0.0
    for a, b in zip(closes[:n], closes[1:n + 1]):
        d = b - a
        g += max(d, 0); l += max(-d, 0)
    g /= n; l /= n
    for a, b in zip(closes[n:], closes[n + 1:]):
        d = b - a
        g = (g * (n - 1) + max(d, 0)) / n
        l = (l * (n - 1) + max(-d, 0)) / n
    return 100.0 if l == 0 else round(100 - 100 / (1 + g / l), 1)


def ema(xs, n):
    k, e, out = 2 / (n + 1), xs[0], []
    for x in xs:
        e = x * k + e * (1 - k)
        out.append(e)
    return out


def macd_hist(closes):
    if len(closes) < 26:
        return None, None
    m = [a - b for a, b in zip(ema(closes, 12), ema(closes, 26))]
    sig = ema(m, 9)
    h = [a - b for a, b in zip(m, sig)]
    return round(h[-1], 1), round(h[-2], 1)


def bar_view(token, code, hhmm):
    """1분봉 요약: RSI·MACD 막대·최근 3봉 색"""
    try:
        bars = minute_bars(token, code, hhmm.replace(":", "") + "00")
    except Exception:
        return None
    if len(bars) < 15:
        return None
    cl = [b["c"] for b in bars]
    h1, h0 = macd_hist(cl)
    colors = "".join("🟥" if b["c"] > b["o"] else ("🟦" if b["c"] < b["o"] else "⬜") for b in bars[-3:])
    return {"rsi": rsi(cl), "macd": h1, "macd_prev": h0, "last3": colors, "n": len(bars)}


def view_text(v):
    if not v:
        return ""
    mac = "" if v["macd"] is None else f" · MACD막대 {v['macd']:+}({'↑' if v['macd'] > (v['macd_prev'] or 0) else '↓'})"
    return f"\n1분봉 최근 3개 {v['last3']} · RSI {v['rsi']}{mac}"


def make_plan(s, p, rng, cash):
    """시가 범위 상단 돌파 후 2~3분 유지됐을 때의 진입 계획 (롱만)"""
    etf = is_etf(s["name"])
    t = tick(p["price"], etf)
    hi, lo = rng
    # 돌파가 근처에서만 산다(플레이북 규칙 12): 돌파가 +0.5% 넘게 올라가 있으면 돌파가 +0.3% 지정가로 대기
    chase = p["price"] > hi * 1.005
    entry = rnd(hi * 1.003, t, True) if chase else max(p["price"], hi + t)
    entry = rnd(entry, t, True)
    width = (hi - lo) / hi if hi else 0
    if etf or width <= 0.03:
        stop = lo - tick(lo, etf)
    else:  # 범위가 넓으면 중간값
        stop = rnd((hi + lo) / 2, tick(lo, etf))
    R = entry - stop
    if R <= 0:
        return None
    qty = min(RISK_PER_TRADE // R, int(cash * 0.5) // entry)
    if qty < 1:
        return None
    target = rnd(entry + 2 * R, t, True)
    return {"code": s["code"], "name": s["name"], "entry": entry, "stop": stop, "target": target,
            "qty": int(qty), "R": R, "risk": int(qty * R), "trail": trail_for(entry, stop, etf), "chase": chase}


def plan_text(pl):
    lines = [f"진입가(지정가) {pl['entry']:,}원 · {pl['qty']}주 · 손절 {pl['stop']:,} · 익절 {pl['target']:,}",
             f"1R {pl['R']:,}원 · 위험 약 {pl['risk']:,}원"]
    for tr in pl["trail"]:
        lines.append(f"🔼 {tr['at']:,} 도달 → 손절 {tr['stop']:,}")
    if pl.get("chase"):
        lines.append("⚠️ 이미 돌파가보다 많이 올라 있음 → 위 지정가로 걸고 10분 안에 안 사지면 취소(쫓지 않기)")
    lines.append(f"체결되면 텔레그램에: /buy {pl['code']} {pl['entry']} {pl['qty']}")
    return "\n".join(lines)


def read_trades():
    if not os.path.exists(TRADES_PATH):
        return []
    return list(csv.DictReader(open(TRADES_PATH, encoding="utf-8")))


def pnl_summary(today):
    d = datetime.date.fromisoformat(today)
    monday = (d - datetime.timedelta(days=d.weekday())).isoformat()
    tr = read_trades()
    day = sum(int(x["pnl"]) for x in tr if x["date"] == today)
    week = sum(int(x["pnl"]) for x in tr if monday <= x["date"] <= today)
    return day, week


def apply_positions(cfg, pos):
    """positions.json의 보유 내역을 watchlist에 덮어씀 (봇과 Claude가 같은 파일을 덜 겹치게 수정)"""
    by = {s["code"]: s for s in cfg["stocks"]}
    for code, h in pos.get("positions", {}).items():
        s = by.get(code)
        if not s:
            s = {"name": h["name"], "code": code}
            cfg["stocks"].append(s)
            by[code] = s
        s.update({"type": "보유", "buy_price": h["buy_price"], "qty": h["qty"], "stop": h["stop"],
                  "target": h["target"], "trail": h.get("trail", []), "orb": False})
        s.setdefault("notes", {})
        s["notes"] = dict(s["notes"], caution="", trail="조건주문 손절가를 이 가격으로 수정(익절은 그대로). 수정 후 /stop 코드 가격",
                          target="🎯 익절 조건주문 체결 확인. 매도 후 /sell 코드 가격",
                          stop="손절 체결 확인. 매도 후 /sell 코드 가격. 오늘 이 종목 재매수 금지",
                          eod="15:15 당일 청산: 남은 수량 시장가 정리 후 /sell 코드 가격")


def tg_commands(today, hhmm, cfg, plans):
    """텔레그램으로 받은 /buy /sell /stop /status 처리 (본세 채팅방 메시지만)"""
    off_path = f"{STATE_DIR}/tg_offset.json"
    off = json.load(open(off_path))["offset"] if os.path.exists(off_path) else 0
    try:
        r = requests.get(f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates",
                         params={"offset": off + 1, "timeout": 0}, timeout=10)
        ups = r.json().get("result", [])
    except Exception:
        return []
    out, changed = [], False
    pos = load_pos()
    names = {s["code"]: s["name"] for s in cfg["stocks"]}
    nowts = time.time()
    for u in ups:
        off = max(off, u["update_id"])
        m = u.get("message") or {}
        if str(m.get("chat", {}).get("id")) != str(TG_CHAT) or nowts - m.get("date", 0) > 900:
            continue
        parts = (m.get("text") or "").strip().split()
        if not parts:
            continue
        cmd = parts[0].lower().split("@")[0]
        try:
            if cmd in ("/buy", "매수"):
                code, price, qty = parts[1], int(parts[2].replace(",", "")), int(parts[3])
                pl = plans.get(code)
                stop = int(parts[4].replace(",", "")) if len(parts) > 4 else (pl or {}).get("stop")
                if not stop:
                    out.append(f"❓ {code} 손절가를 모르겠어요. /buy {code} {price} {qty} 손절가 익절가 형식으로 보내 주세요")
                    continue
                R = price - stop
                name = names.get(code, (pl or {}).get("name", code))
                etf = is_etf(name)
                target = int(parts[5].replace(",", "")) if len(parts) > 5 else rnd(price + 2 * R, tick(price, etf), True)
                tr = trail_for(price, stop, etf)
                pos["positions"][code] = {"name": name, "buy_price": price, "qty": qty, "stop": stop,
                                          "target": target, "trail": tr, "R0": R, "opened": f"{today} {hhmm}"}
                pos.setdefault("entries", {})
                pos["entries"][today] = pos["entries"].get(today, 0) + 1
                changed = True
                lines = [f"✅ 보유 등록: {name} {qty}주 @{price:,}",
                         f"조건주문(익절·손절, 시장가, 당일): {target:,}원 이상 / {stop:,}원 이하",
                         f"1R {R:,}원 · 위험 약 {R*qty:,}원"]
                lines += [f"🔼 {x['at']:,} 도달 → 손절 {x['stop']:,}" for x in tr]
                lines.append(f"오늘 진입 {pos['entries'][today]}/{MAX_ENTRIES}회")
                out.append("\n".join(lines))
            elif cmd in ("/sell", "매도"):
                code, price = parts[1], int(parts[2].replace(",", ""))
                h = pos["positions"].get(code)
                if not h:
                    out.append(f"❓ {code} 보유 기록이 없어요")
                    continue
                qty = int(parts[3]) if len(parts) > 3 else h["qty"]
                pnl = (price - h["buy_price"]) * qty
                R0 = h.get("R0") or (h["buy_price"] - h["stop"]) or 1
                os.makedirs("data", exist_ok=True)
                new = not os.path.exists(TRADES_PATH)
                with open(TRADES_PATH, "a", newline="", encoding="utf-8") as f:
                    w = csv.writer(f)
                    if new:
                        w.writerow(["date", "time", "code", "name", "buy", "sell", "qty", "pnl", "R"])
                    w.writerow([today, hhmm, code, h["name"], h["buy_price"], price, qty, pnl,
                                round((price - h["buy_price"]) / R0, 2)])
                pos["cash"] = pos.get("cash", 0) + pnl
                if qty >= h["qty"]:
                    del pos["positions"][code]
                else:
                    h["qty"] -= qty
                changed = True
                day, week = pnl_summary(today)
                out.append(f"📒 매도 기록: {h['name']} {qty}주 @{price:,} → {pnl:+,}원\n오늘 {day:+,}원 · 이번 주 {week:+,}원 · 현금 약 {pos['cash']:,}원")
            elif cmd in ("/stop", "손절"):
                code, stop = parts[1], int(parts[2].replace(",", ""))
                h = pos["positions"].get(code)
                if not h:
                    out.append(f"❓ {code} 보유 기록이 없어요")
                    continue
                h.setdefault("R0", h["buy_price"] - h["stop"])
                h["stop"] = stop
                h["trail"] = [x for x in h.get("trail", []) if x["stop"] > stop]
                changed = True
                out.append(f"🔼 {h['name']} 손절가 {stop:,}원으로 기록")
            elif cmd in ("/status", "상태"):
                day, week = pnl_summary(today)
                hs = [f"- {h['name']} {h['qty']}주 @{h['buy_price']:,} 손절 {h['stop']:,} 익절 {h['target']:,}"
                      for h in pos["positions"].values()] or ["- 보유 없음"]
                out.append("\n".join(["📋 현황"] + hs + [
                    f"오늘 {day:+,}원 (한도 {DAY_LIMIT:,}) · 이번 주 {week:+,}원 (한도 {WEEK_LIMIT:,})",
                    f"오늘 진입 {pos.get('entries', {}).get(today, 0)}/{MAX_ENTRIES}회 · 현금 약 {pos.get('cash', 0):,}원"]))
            elif cmd in ("/help", "도움말"):
                out.append("명령어\n/buy 코드 체결가 수량 [손절가 익절가]\n/sell 코드 체결가 [수량]\n/stop 코드 새손절가\n/status")
        except (IndexError, ValueError):
            out.append("❓ 형식 확인: /buy 122630 59190 7  ·  /sell 122630 61940  ·  /stop 122630 59880")
    json.dump({"offset": off}, open(off_path, "w"))
    if changed:
        save_pos(pos)
    return out


def main():
    mode = os.environ.get("MODE", "check")
    if mode == "message":  # 텍스트를 그대로 텔레그램으로 전송
        tg(os.environ.get("MESSAGE", "").replace("\\n", "\n"))
        return
    now = datetime.datetime.now(KST)
    today = now.strftime("%Y-%m-%d")
    hhmm = now.strftime("%H:%M")
    token = get_token()
    if mode == "premarket":
        premarket(token, today)
        return
    if mode == "minutetest":  # 1분봉 API 동작 확인용
        os.makedirs("data", exist_ok=True)
        with open("data/minute_debug.txt", "a", encoding="utf-8") as f:
            for code in ("122630", "114800", "005930"):
                try:
                    b = minute_bars(token, code, now.strftime("%H%M%S") if hhmm < "15:30" else "153000")
                    f.write(f"{today} {hhmm} {code} n={len(b)} first={b[:1]} last={b[-1:]} view={bar_view(token, code, hhmm if hhmm < '15:30' else '15:30')}\n")
                except Exception as e:
                    f.write(f"{today} {hhmm} {code} ERROR {e}\n")
                time.sleep(0.6)
        return
    cfg = json.load(open("watchlist.json", encoding="utf-8"))
    plans_path = f"{STATE_DIR}/plans_{today}.json"
    plans = json.load(open(plans_path)) if os.path.exists(plans_path) else {}
    pre_msgs = tg_commands(today, hhmm, cfg, plans) if mode == "check" else []
    pos = load_pos()
    apply_positions(cfg, pos)

    # 시가 범위(09:00~09:30 고가·저가): 09:30까지 계속 갱신, 이후 고정
    or_path = f"{STATE_DIR}/or_{today}.json"
    orng = json.load(open(or_path)) if os.path.exists(or_path) else {}
    alert_path = f"{STATE_DIR}/alerts_{today}.json"
    sent = json.load(open(alert_path)) if os.path.exists(alert_path) else []
    rows, msgs = [], list(pre_msgs)
    quotes = {}
    idx_now = {}
    pend_path = f"{STATE_DIR}/pending_{today}.json"
    pending = json.load(open(pend_path)) if os.path.exists(pend_path) else {}

    for idx in cfg.get("indices", []):
        p = index_price(token, idx["code"])
        time.sleep(0.6)
        if p:
            rows.append([hhmm, idx["name"], p["price"], p["change_pct"], "", "", "", ""])
            idx_now[idx["code"]] = p["price"]
            if hhmm <= "09:30":
                k = "IDX_" + idx["code"]
                h0, l0 = orng.get(k, [p["price"], p["price"]])
                hi_ = max(h0, p["high"] or p["price"], p["price"])
                lo_ = min(l0, p["low"] or p["price"], p["price"])
                orng[k] = [hi_, lo_]

    for s in cfg["stocks"]:
        p = stock_price(token, s["code"])
        quotes[s["code"]] = p
        time.sleep(0.6)
        rows.append([hhmm, s["name"], p["price"], p["change_pct"], p["volume"], p["vwap"], p["high"], p["low"]])
        if hhmm <= "09:30" and p["high"]:
            orng[s["code"]] = [p["high"], p["low"]]

        checks = []
        tag = f"[{s.get('type', '보유')}] "
        if s.get("stop") and p["price"] <= s["stop"]:
            checks.append(("stop", f"🚨 {s['name']} 손절선 {s['stop']:,}원 이탈!\n현재 {p['price']:,}원 ({p['change_pct']:+.2f}%)"))
        # 보유 종목: 손절선 1% 위에 오면 미리 주의 알림 (손절가를 본전 이상으로 올린 뒤에는 생략)
        if s.get("type") == "보유" and s.get("stop") and s["stop"] < s.get("buy_price", s["stop"] + 1) and s["stop"] < p["price"] <= s["stop"] * 1.01:
            checks.append(("caution", f"🟠 {s['name']} 손절선 근접 ({s['stop']:,}원까지 {p['price'] - s['stop']:,}원)\n현재 {p['price']:,}원 ({p['change_pct']:+.2f}%)"))
        # 보유 종목: 수익이 나면 손절가를 올리라고 알림 (trail: [{"at":가격,"stop":새 손절가}])
        if s.get("type") == "보유":
            for t in s.get("trail", []):
                if p["price"] >= t["at"] and (s.get("stop") or 0) < t["stop"]:
                    checks.append((f"trail{t['at']}", f"🔼 {s['name']} {t['at']:,}원 도달 → 손절가를 {t['stop']:,}원으로 올리세요\n현재 {p['price']:,}원 ({p['change_pct']:+.2f}%) · 토스 조건주문 손절가 수정"))
        lo, hi = s.get("entry_low"), s.get("entry_high")
        if lo and hi and lo <= p["price"] <= hi:
            checks.append(("entry", f"🟢 {s['name']} 진입 구간 도달 ({lo:,}~{hi:,}원)\n현재 {p['price']:,}원 ({p['change_pct']:+.2f}%) · 손절 {s.get('stop') or 0:,} / 목표 {s.get('target') or 0:,}"))
        if s.get("target") and p["price"] >= s["target"]:
            checks.append(("target", f"🎯 {s['name']} 목표가 {s['target']:,}원 도달!\n현재 {p['price']:,}원 ({p['change_pct']:+.2f}%)"))
        # 시가 범위 돌파 신호 (orb: true 종목) — 09:30 이후, VWAP과 같은 방향일 때만
        rng = orng.get(s["code"])
        if s.get("orb") and rng and hhmm > "09:30" and p["vwap"]:
            above = p["price"] > rng[0] and p["price"] > p["vwap"]
            if above:
                checks.append(("orb_up", f"📈 {s['name']} 시가 범위 상단 돌파 ({rng[0]:,}원)\n현재 {p['price']:,}원 · VWAP {p['vwap']:,} ({p['change_pct']:+.2f}%)"))
            # 2~3분 유지 확인 → ✅ 진입 계획 / 되돌아오면 ↩️ 가짜 돌파
            pd = pending.get(s["code"])
            if above and not pd:
                pending[s["code"]] = {"t": hhmm, "level": rng[0]}
            elif above and pd and pd["t"] != hhmm and f"{s['code']}_confirm" not in sent:
                why = []
                ic = INDEX_COND.get(s["code"])
                if ic:
                    ir, iv = orng.get("IDX_" + ic[0]), idx_now.get(ic[0])
                    if ir and iv is not None:
                        if ic[1] == "below_low" and not iv < ir[1]:
                            why.append(f"지수가 시가 범위 하단({ir[1]:,.2f}) 아래가 아님")
                        if ic[1] == "above_mid" and not iv > (ir[0] + ir[1]) / 2:
                            why.append("지수가 시가 범위 중간 아래")
                if hhmm > "14:30":
                    why.append("14:30 이후")
                if pos.get("entries", {}).get(today, 0) >= MAX_ENTRIES:
                    why.append(f"오늘 진입 {MAX_ENTRIES}회 다 씀")
                dsum, wsum = pnl_summary(today)
                if dsum <= DAY_LIMIT or wsum <= WEEK_LIMIT:
                    why.append("손실 한도 도달")
                if s.get("type") == "보유":
                    why.append("이미 보유 중")
                if s.get("type") == "관심":
                    why.append("일봉 20일선 확인 필요(관심 종목)")
                pl = make_plan(s, p, rng, pos.get("cash", 1100000))
                if pl is None:
                    why.append("손절 폭 대비 수량이 안 나옴")
                sent.append(f"{s['code']}_confirm")
                if why:
                    msgs.append(f"[{s.get('type')}] ⏸ {s['name']} 돌파 유지 확인, 하지만 진입 보류: " + " / ".join(why) + f"\n현재 {p['price']:,}원")
                else:
                    plans[s["code"]] = pl
                    v = bar_view(token, s["code"], hhmm)
                    hot = " ⚠️ RSI 80 이상(과열) → 지정가로 눌림 대기" if v and v["rsi"] and v["rsi"] >= 80 else ""
                    msgs.append(f"[{s.get('type')}] ✅ {s['name']} 돌파 2분 이상 유지 → 진입 가능\n현재 {p['price']:,}원 · VWAP {p['vwap']:,}{view_text(v)}{hot}\n" + plan_text(pl))
            elif not above and pd:
                pending.pop(s["code"], None)
                if p["price"] <= rng[0] and f"{s['code']}_fake" not in sent and f"{s['code']}_confirm" not in sent:
                    sent.append(f"{s['code']}_fake")
                    msgs.append(f"[{s.get('type')}] ↩️ {s['name']} 가짜 돌파: {rng[0]:,}원 아래로 복귀 ({p['price']:,}원) → 진입하지 마세요")
            elif p["price"] < rng[1] and p["price"] < p["vwap"]:
                checks.append(("orb_down", f"📉 {s['name']} 시가 범위 하단 이탈 ({rng[1]:,}원)\n현재 {p['price']:,}원 · VWAP {p['vwap']:,} ({p['change_pct']:+.2f}%)"))
        # 단타 원칙: 보유 종목은 15:15에 당일 청산 알림
        if s.get("type") == "보유" and hhmm >= "15:15":
            checks.append(("eod", f"⏰ {s['name']} 당일 청산 시간 (15:15)\n현재 {p['price']:,}원 · 매수가 {s.get('buy_price') or 0:,} · 오버나이트 금지"))
        lim = cfg.get("move_alert_pct", 5)
        if abs(p["change_pct"]) >= lim and s.get("type", "보유") in ("보유", "관심"):
            word = "급등" if p["change_pct"] > 0 else "급락"
            checks.append(("move", f"⚡ {s['name']} {word} {p['change_pct']:+.2f}%\n현재 {p['price']:,}원"))

        for key, msg in checks:
            # 기준가를 장중에 바꾸면 같은 날이라도 새 기준으로 다시 알림
            lv = {"entry": s.get("entry_low"), "stop": s.get("stop"), "target": s.get("target"), "caution": s.get("stop")}.get(key, "")
            k = f"{s['code']}_{key}_{lv}"
            if k not in sent:  # 같은 알림은 하루 한 번만
                note = (s.get("notes") or {}).get("trail" if key.startswith("trail") else key) or s.get("note")
                if key == "caution":
                    note = f"손절선 근접. 조건주문 손절가({s['stop']:,}) 걸려 있는지 확인. 추가 매수 X. 계획 손절가 외 즉석 정리 X."
                msgs.append(tag + msg + (f"\n💬 Claude: {note}" if note else ""))
                sent.append(k)

    if mode == "check" and "09:30" <= hhmm <= "14:30":
        mine = {s["code"] for s in cfg["stocks"]}
        etf = ("KODEX", "TIGER", "ACE", "RISE", "SOL", "PLUS", "KOSEF", "HANARO", "KIWOOM", "1Q", "미래에셋")
        n_surge = 0
        for i, x in enumerate(amount_rank(token, "J", 15)):
            k = f"surge_{x['code']}"
            if n_surge >= 2 or x["name"].startswith(etf):
                continue
            if x["code"] not in mine and abs(x["chg"]) >= 7 and k not in sent:
                n_surge += 1
                msgs.append(f"🔥 거래대금 상위 {i+1}위 신규: {x['name']}({x['code']}) {x['chg']:+.2f}% · {x['price']:,}원 · 대금 {x['amount']//100000000:,}억\n💬 Claude: 이슈 확인 전 추격 X. 캡처 보내 주시면 시가 범위·VWAP로 판단")
                sent.append(k)

    json.dump(sent, open(alert_path, "w"))
    json.dump(orng, open(or_path, "w"))
    json.dump(pending, open(pend_path, "w"))
    json.dump(plans, open(plans_path, "w"))

    os.makedirs("data", exist_ok=True)
    path = f"data/{today}.csv"
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time", "name", "price", "change_pct", "volume", "vwap", "high", "low"])
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
