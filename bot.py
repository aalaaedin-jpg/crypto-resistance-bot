#!/usr/bin/env python3
"""
Futures resistance scanner + Telegram alerts (Gate.io public data, no API key needed).

Every INTERVAL seconds it scans the top crypto USDT perpetuals, computes the
"rise score /10" and, for any coin with score >= THRESHOLD, sends a Telegram
message with 3 resistance levels above and the nearest support below.

Usage:
  python3 bot.py --get-chat-id     # after sending any message to your bot, prints your chat id
  python3 bot.py --test            # sends a test message to Telegram
  python3 bot.py --once --dry      # one scan, print to terminal only (no Telegram)
  python3 bot.py                   # run forever, scan every 15 min, alert on Telegram

Settings come from config.json next to this file (see config.example.json).
The score is a hand-made heuristic, NOT a probability, and has not been backtested.
"""
import json, os, sys, time, datetime, urllib.request, urllib.parse, concurrent.futures as cf

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = {"telegram_token": "", "chat_id": "", "threshold": 8.0, "interval_sec": 900,
       "top_n": 130, "cooldown_min": 120}
if os.path.exists(os.path.join(HERE, "config.json")):
    CFG.update(json.load(open(os.path.join(HERE, "config.json"))))
# environment variables (used by GitHub Actions secrets) override config.json
for key, env, cast in [("telegram_token", "TELEGRAM_TOKEN", str), ("chat_id", "CHAT_ID", str),
                       ("threshold", "THRESHOLD", float)]:
    if os.environ.get(env): CFG[key] = cast(os.environ[env])

STATE = os.path.join(HERE, "state.json")  # remembers when each coin was last alerted
def load_state():
    try: return json.load(open(STATE))
    except Exception: return {}
def save_state(s):
    json.dump(s, open(STATE, "w"))

BASE = "https://api.gateio.ws/api/v4/futures/usdt"

# ---------------------------------------------------------------- data
def get(path, tries=3):
    for i in range(tries):
        try:
            with urllib.request.urlopen(BASE + path, timeout=40) as r:
                return json.loads(r.read())
        except Exception:
            if i == tries - 1: raise
            time.sleep(3 * (i + 1))

def candles(c, iv, limit):
    d = get(f"/candlesticks?contract={c}&interval={iv}&limit={limit}")
    return [dict(o=float(x["o"]), h=float(x["h"]), l=float(x["l"]), c=float(x["c"]),
                 v=float(x.get("sum") or x["v"])) for x in d]

_ctype = {}
def is_crypto(c):
    if c not in _ctype:
        _ctype[c] = not get(f"/contracts/{c}").get("contract_type")  # stocks/commodities have a type
    return _ctype[c]

# ---------------------------------------------------------------- indicators
def rsi(closes, n=14):
    g = l = 0.0
    for i in range(1, n + 1):
        ch = closes[i] - closes[i - 1]; g += max(ch, 0); l += max(-ch, 0)
    ag, al = g / n, l / n
    for i in range(n + 1, len(closes)):
        ch = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(ch, 0)) / n; al = (al * (n - 1) + max(-ch, 0)) / n
    return 100 if al == 0 else 100 - 100 / (1 + ag / al)

def pivots(cs, k, key, fn):
    return [cs[i][key] for i in range(k, len(cs) - k)
            if cs[i][key] == fn(x[key] for x in cs[i - k:i + k + 1])]

def cluster(levels, tol=0.006):
    groups = []
    for lv in sorted(levels):
        if groups and abs(lv - groups[-1][-1]) / lv <= tol: groups[-1].append(lv)
        else: groups.append([lv])
    return [(sum(g) / len(g), len(g)) for g in groups]

def atr(cs, n=14):
    trs = [max(cs[i]["h"] - cs[i]["l"], abs(cs[i]["h"] - cs[i-1]["c"]), abs(cs[i]["l"] - cs[i-1]["c"]))
           for i in range(1, len(cs))]
    return sum(trs[-n:]) / n

# ---------------------------------------------------------------- score
cl = lambda x: max(0.0, min(1.0, x))
def mom(r):
    if r >= 75: return 0.2
    if r >= 50: return 1 - (r - 50) / 40
    if r >= 30: return (r - 30) / 20 * 0.8
    return 0.3

def rise_score(dist, touches, rej, r15, r1h, r4h, vol):
    room = 2.0 if dist < 0 else cl(dist / 1.5) * 2
    weak = (1 - cl((touches + rej) / 30)) * 2
    volp = cl((vol - 0.5) / 2.5) * 2
    return room + weak + volp + mom(r15) * 1.5 + mom(r1h) * 1.5 + mom(r4h) * 1.0

# ---------------------------------------------------------------- analysis
def analyse(c):
    try:
        if not is_crypto(c): return None
        c15 = candles(c, "15m", 300); c1h = candles(c, "1h", 500); c4h = candles(c, "4h", 300)
    except Exception:
        return None
    if len(c15) < 100 or len(c1h) < 100 or len(c4h) < 30: return None
    price = c15[-1]["c"]

    # nearest strong resistance (same rules as the manual scan)
    highs = pivots(c1h[-300:], 3, "h", max) + pivots(c15[:-3], 4, "h", max)
    tol, best = 0.004, None
    for lvl in set(highs):
        if not (price * 0.997 <= lvl <= price * 1.015): continue
        zone = [h for h in highs if abs(h - lvl) / lvl <= tol]
        center = sum(zone) / len(zone)
        rej = sum(1 for x in c1h[-300:] if abs(x["h"] - center) / center <= tol and x["c"] < center * 0.997)
        if best is None or (len(zone), rej) > best[:2]: best = (len(zone), rej, center)
    if not best: return None
    touches, rej, lvl = best

    vols = [x["v"] for x in c15]
    vol = (sum(vols[-4:]) / 4) / (sum(vols[-52:-4]) / 48 + 1e-9)
    r15, r1h, r4h = rsi([x["c"] for x in c15]), rsi([x["c"] for x in c1h]), rsi([x["c"] for x in c4h])
    dist = (lvl - price) / price * 100
    score = rise_score(dist, touches, rej, r15, r1h, r4h, vol)

    # levels above / below
    a = atr(c1h)
    hi = cluster(pivots(c1h, 3, "h", max) + pivots(c4h, 2, "h", max) + [max(x["h"] for x in c4h)])
    lo = cluster(pivots(c1h, 3, "l", min) + pivots(c4h, 2, "l", min) + [min(x["l"] for x in c4h)])
    up = [lv for lv, _ in hi if lv > price * 1.002][:3]
    dn = sorted([lv for lv, _ in lo if lv < price * 0.998], reverse=True)
    s1 = dn[0] if dn else None
    return dict(coin=c.replace("_USDT", ""), price=price, dist=dist, touches=touches, rej=rej,
                r15=r15, r1h=r1h, r4h=r4h, vol=vol, score=score, up=up, s1=s1,
                below=(s1 - a * 0.5) if s1 else None, atr_pct=a / price * 100)

def scan():
    tick = [t for t in get("/tickers") if t["contract"].endswith("_USDT")]
    tick.sort(key=lambda t: float(t.get("volume_24h_quote") or 0), reverse=True)
    names = [t["contract"] for t in tick[:CFG["top_n"]]]
    with cf.ThreadPoolExecutor(6) as ex:
        res = [r for r in ex.map(analyse, names) if r]
    return sorted(res, key=lambda r: -r["score"])

# ---------------------------------------------------------------- telegram
def tg(method, params):
    url = f"https://api.telegram.org/bot{CFG['telegram_token']}/{method}"
    data = urllib.parse.urlencode(params).encode()
    with urllib.request.urlopen(url, data=data, timeout=20) as r:
        return json.loads(r.read())

def send(text, silent=False):
    return tg("sendMessage", {"chat_id": CFG["chat_id"], "text": text, "parse_mode": "HTML",
                              "disable_web_page_preview": "true",
                              "disable_notification": "true" if silent else "false"})

f6 = lambda x: f"{x:.6g}"
def pct(lv, p): return f"{(lv / p - 1) * 100:+.1f}%"

def message(r):
    p = r["price"]
    lines = [f"🚀 <b>{r['coin']}</b>  مؤشر الصعود <b>{r['score']:.1f}/10</b>",
             f"السعر: {f6(p)}",
             f"RSI 15m/1h/4h: {r['r15']:.0f} / {r['r1h']:.0f} / {r['r4h']:.0f}  |  فوليوم ×{r['vol']:.2f}",
             f"المقاومة القريبة: {pct(p*(1+r['dist']/100), p)}  (لمسات {r['touches']}, رفض {r['rej']})"]
    for i, lv in enumerate(r["up"], 1):
        lines.append(f"هدف {i}: {f6(lv)} ({pct(lv, p)})")
    if r["s1"]:
        lines.append(f"أقرب دعم: {f6(r['s1'])} ({pct(r['s1'], p)})")
        lines.append(f"تحت الدعم (S1−½ATR): {f6(r['below'])} ({pct(r['below'], p)})")
    if r["atr_pct"] > 8:
        lines.append(f"⚠️ تذبذب عالي جدًا (ATR 1h = {r['atr_pct']:.1f}%)")
    lines.append("<i>مؤشر حسابي مش احتمال، ومش توصية.</i>")
    return "\n".join(lines)

# ---------------------------------------------------------------- main
def cairo_now():
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo("Africa/Cairo"))
    except Exception:
        return datetime.datetime.now()

def summary(res):
    lines = [f"🔎 فحص {cairo_now().strftime('%H:%M')}: ملقيتش عملة مؤشرها {CFG['threshold']:g} أو أكتر.",
             f"عدد العملات القريبة من مقاومة: {len(res)}"]
    if res:
        lines.append("أعلى 5:")
        for r in res[:5]:
            lines.append(f"• {r['coin']}  {r['score']:.1f}/10  (المقاومة {r['dist']:+.1f}%)")
    return "\n".join(lines)

def run_once(dry=False, last_sent=None):
    stamp = cairo_now().strftime("%H:%M")
    res = scan()
    hits = [r for r in res if r["score"] >= CFG["threshold"]]
    top = ", ".join(f"{r['coin']} {r['score']:.1f}" for r in res[:5]) or "-"
    print(f"[{stamp}] {len(res)} near resistance | top: {top} | alerts: {len(hits)}", flush=True)
    sent = 0
    for r in hits:
        if last_sent is not None and time.time() - last_sent.get(r["coin"], 0) < CFG["cooldown_min"] * 60:
            continue
        sent += 1
        if dry: print(message(r), "\n")
        else:
            send(message(r))
            if last_sent is not None: last_sent[r["coin"]] = time.time()
    if sent == 0:  # nothing new: still report that the scan ran (silent notification)
        if dry: print(summary(res))
        else: send(summary(res), silent=True)

def main():
    a = sys.argv[1:]
    if "--get-chat-id" in a:
        for u in tg("getUpdates", {}).get("result", []):
            m = u.get("message") or u.get("channel_post") or {}
            if m: print("chat_id:", m["chat"]["id"], "|", m["chat"].get("username") or m["chat"].get("title"))
        return
    if "--test" in a:
        print(send("✅ البوت شغال ومتوصل.")["ok"]); return
    if "--once" in a:
        dry = "--dry" in a
        state = None if dry else load_state()
        run_once(dry=dry, last_sent=state)
        if state is not None: save_state(state)
        return
    if not CFG["telegram_token"] or not CFG["chat_id"]:
        sys.exit("Fill telegram_token and chat_id in config.json first.")
    last_sent = {}
    print(f"Running every {CFG['interval_sec']//60} min, threshold {CFG['threshold']}. Ctrl+C to stop.")
    while True:
        t0 = time.time()
        try: run_once(last_sent=last_sent)
        except Exception as e: print("error:", e, flush=True)
        time.sleep(max(30, CFG["interval_sec"] - (time.time() - t0)))

if __name__ == "__main__":
    main()
