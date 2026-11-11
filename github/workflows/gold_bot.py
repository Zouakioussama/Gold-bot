import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests
import yfinance as yf
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
SYMBOL = os.getenv("SYMBOL", "GC=F")
CHECK_MINUTES = int(os.getenv("CHECK_MINUTES", "15"))
MIN_SCORE = int(os.getenv("MIN_SCORE", "4"))
COOLDOWN_HOURS = float(os.getenv("COOLDOWN_HOURS", "4"))
ATR_SL_MULT = float(os.getenv("ATR_SL_MULT", "1.5"))
ATR_TP_MULT = float(os.getenv("ATR_TP_MULT", "3.0"))
NEWS_BLOCK_MIN = int(os.getenv("NEWS_BLOCK_MIN", "30"))
STATE_FILE = os.getenv("STATE_FILE", "state.json")
CAL_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("gold-bot")


def fetch(symbol, interval, period):
    df = yf.download(symbol, interval=interval, period=period,
                     progress=False, auto_adjust=True)
    if df is None or df.empty:
        raise RuntimeError(f"No data for {symbol} ({interval})")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df.dropna()


def to_4h(df1h):
    return df1h.resample("4h").agg(
        {"Open": "first", "High": "max", "Low": "min", "Close": "last"}
    ).dropna()


def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def rsi(s, n=14):
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn.replace(0, 1e-12)
    return 100 - 100 / (1 + rs)


def macd_hist(s):
    line = ema(s, 12) - ema(s, 26)
    return line - line.ewm(span=9, adjust=False).mean()


def atr(df, n=14):
    pc = df["Close"].shift()
    tr = pd.concat([df["High"] - df["Low"],
                    (df["High"] - pc).abs(),
                    (df["Low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def technical_score(df1h, df4h):
    c = df1h["Close"]
    price = float(c.iloc[-1])
    e50, e200 = float(ema(c, 50).iloc[-1]), float(ema(c, 200).iloc[-1])
    score, why = 0, []

    if price > e50 > e200:
        score += 2; why.append("ترند صاعد على 1H (السعر > EMA50 > EMA200)")
    elif price < e50 < e200:
        score -= 2; why.append("ترند هابط على 1H (السعر < EMA50 < EMA200)")
    else:
        why.append("ترند 1H غير واضح")

    c4 = df4h["Close"]
    if c4.iloc[-1] > ema(c4, 50).iloc[-1]:
        score += 1; why.append("4H فوق EMA50")
    else:
        score -= 1; why.append("4H تحت EMA50")

    h = macd_hist(c)
    if h.iloc[-1] > 0 and h.iloc[-1] > h.iloc[-2]:
        score += 1; why.append("MACD إيجابي ويزيد")
    elif h.iloc[-1] < 0 and h.iloc[-1] < h.iloc[-2]:
        score -= 1; why.append("MACD سلبي وينقص")

    r = float(rsi(c).iloc[-1])
    if r > 75:
        score -= 1; why.append(f"RSI={r:.0f} تشبع شراء")
    elif r < 25:
        score += 1; why.append(f"RSI={r:.0f} تشبع بيع")
    elif r > 55:
        score += 1; why.append(f"RSI={r:.0f} زخم صاعد")
    elif r < 45:
        score -= 1; why.append(f"RSI={r:.0f} زخم هابط")

    return score, why, price, float(atr(df1h).iloc[-1])


def fundamental_score():
    score, why = 0, []
    dxy = fetch("DX-Y.NYB", "1d", "1mo")["Close"]
    chg = (dxy.iloc[-1] / dxy.iloc[-6] - 1) * 100
    if chg < -0.3:
        score += 1; why.append(f"الدولار (DXY) كينقص {chg:.2f}% / 5 أيام")
    elif chg > 0.3:
        score -= 1; why.append(f"الدولار (DXY) كيزيد {chg:+.2f}% / 5 أيام")

    y10 = fetch("^TNX", "1d", "1mo")["Close"]
    dy = y10.iloc[-1] - y10.iloc[-6]
    if dy < -0.10:
        score += 1; why.append(f"عائد السندات 10Y كينقص ({dy:+.2f})")
    elif dy > 0.10:
        score -= 1; why.append(f"عائد السندات 10Y كيزيد ({dy:+.2f})")

    return score, why


def high_impact_news_nearby():
    try:
        events = requests.get(CAL_URL, timeout=15).json()
    except Exception as exc:
        log.warning("Calendar fetch failed: %s", exc)
        return None
    now = datetime.now(timezone.utc)
    for e in events:
        if e.get("country") == "USD" and e.get("impact") == "High":
            t = datetime.fromisoformat(e["date"]).astimezone(timezone.utc)
            if abs((t - now).total_seconds()) <= NEWS_BLOCK_MIN * 60:
                return e
    return None


def analyze():
    df1h = fetch(SYMBOL, "1h", "60d")
    ts = df1h.index[-1]
    ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
    if datetime.now(timezone.utc) - ts.to_pydatetime() > timedelta(hours=3):
        return None
    df4h = to_4h(df1h)
    t_score, t_why, price, a = technical_score(df1h, df4h)
    f_score, f_why = fundamental_score()
    total = t_score + f_score
    direction = "BUY" if total >= MIN_SCORE else "SELL" if total <= -MIN_SCORE else None
    sign = 1 if direction == "BUY" else -1
    return {
        "price": price, "atr": a, "tech": t_score, "fund": f_score, "total": total,
        "direction": direction, "reasons": t_why + f_why,
        "sl": price - sign * a * ATR_SL_MULT if direction else None,
        "tp": price + sign * a * ATR_TP_MULT if direction else None,
    }


def format_message(r):
    icon = "🟢 شراء" if r["direction"] == "BUY" else "🔴 بيع"
    lines = [
        f"<b>{icon} XAUUSD</b>",
        f"السعر: <b>{r['price']:.2f}</b>",
        f"Stop Loss: {r['sl']:.2f}",
        f"Take Profit: {r['tp']:.2f}  (1:{ATR_TP_MULT / ATR_SL_MULT:.0f})",
        f"السكور: {r['total']:+d} (تقني {r['tech']:+d} / أساسي {r['fund']:+d})",
        "",
        *[f"• {x}" for x in r["reasons"]],
        "",
        "⚠️ مساعدة في القرار وماشي نصيحة مالية. حدد المخاطرة ديالك.",
    ]
    return "\n".join(lines)


def send_telegram(text):
    if not TOKEN or not CHAT_ID:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
    resp = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
        timeout=15,
    )
    resp.raise_for_status()


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def in_cooldown(state, direction):
    last = state.get("last")
    if not last or last["dir"] != direction:
        return False
    sent = datetime.fromisoformat(last["time"])
    return datetime.now(timezone.utc) - sent < timedelta(hours=COOLDOWN_HOURS)


def run_cycle(state, notify=True):
    news = high_impact_news_nearby()
    if news:
        log.info("Skipping: high-impact USD news (%s)", news.get("title"))
        return
    r = analyze()
    if r is None:
        log.info("Market closed / stale data, skipping")
        return
    log.info("price=%.2f score=%+d (tech %+d, fund %+d) -> %s",
             r["price"], r["total"], r["tech"], r["fund"], r["direction"])
    if not r["direction"]:
        return
    if not notify:
        print(format_message(r))
        return
    if in_cooldown(state, r["direction"]):
        log.info("Cooldown active, not re-sending %s", r["direction"])
        return
    send_telegram(format_message(r))
    state["last"] = {"dir": r["direction"], "time": datetime.now(timezone.utc).isoformat()}
    save_state(state)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true")
    p.add_argument("--test", action="store_true")
    p.add_argument("--cycle", action="store_true")
    args = p.parse_args()

    if args.cycle:
        run_cycle(load_state())
        return
    if args.test:
        send_telegram("✅ Gold bot خدام وكيتوصل بيك.")
        return
    if args.once:
        r = analyze()
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return

    state = load_state()
    log.info("Gold bot started: %s every %d min", SYMBOL, CHECK_MINUTES)
    while True:
        try:
            run_cycle(state)
        except Exception as exc:
            log.error("Cycle failed: %s", exc)
        time.sleep(CHECK_MINUTES * 60)


if __name__ == "__main__":
    sys.exit(main())
