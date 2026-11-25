import json
import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd

import gold_bot as g

NY = ZoneInfo("America/New_York")
ICT_MIN_SCORE = int(os.getenv("ICT_MIN_SCORE", "5"))


def mirror(df):
    m = pd.DataFrame(index=df.index)
    m["Open"] = -df["Open"]
    m["High"] = -df["Low"]
    m["Low"] = -df["High"]
    m["Close"] = -df["Close"]
    return m


def swing_low_idx(df, n):
    lo = df["Low"]
    flag = lo == lo.rolling(2 * n + 1, center=True).min()
    return [i for i, v in enumerate(flag.values) if v]


def htf_bias(df, n=2):
    hi_idx = swing_low_idx(mirror(df), n)
    lo_idx = swing_low_idx(df, n)
    if len(hi_idx) < 2 or len(lo_idx) < 2:
        return 0
    H, L = df["High"].values, df["Low"].values
    hh = H[hi_idx[-1]] > H[hi_idx[-2]]
    hl = L[lo_idx[-1]] > L[lo_idx[-2]]
    if hh and hl:
        return 1
    if not hh and not hl:
        return -1
    return 0


def detect_long(df, atr_val, n=3, lookback=40, max_age=16):
    o, h, l, c = (df[x].values for x in ("Open", "High", "Low", "Close"))
    N = len(df)
    for p in reversed(swing_low_idx(df, n)[-8:]):
        s = l[p]
        k = next((i for i in range(p + 1, N) if l[i] < s), None)
        if k is None or k < N - lookback or c[-1] <= s:
            continue
        ref_high = h[p:k].max()
        m = next((i for i in range(k + 1, N) if c[i] > ref_high), None)
        if m is None or (N - 1 - m) > max_age:
            continue
        sweep = l[k:].min()
        fvg = None
        for i in range(N - 1, k, -1):
            if i >= 2 and l[i] > h[i - 2] and (c[i + 1:] > h[i - 2]).all():
                fvg = (h[i - 2], l[i])
                break
        ob = None
        for j in range(m, k - 1, -1):
            if c[j] < o[j]:
                ob = (l[j], h[j])
                break
        zone = fvg or ob
        if zone is None:
            continue
        if c[-1] < zone[0] or c[-1] > zone[1] + 1.5 * atr_val:
            continue
        return {"k": k, "m": m, "sweep": sweep, "fvg": fvg, "ob": ob, "zone": zone}
    return None


def killzone(ts_utc):
    t = ts_utc.astimezone(NY)
    hm = t.hour + t.minute / 60
    if 2 <= hm < 5:
        return "لندن"
    if 7 <= hm < 10:
        return "نيويورك"
    return None


def analyze():
    df15 = g.fetch(g.SYMBOL, "15m", "30d").iloc[:-1]
    ts = df15.index[-1]
    ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
    if datetime.now(timezone.utc) - ts.to_pydatetime() > timedelta(minutes=90):
        return None
    df1h = g.fetch(g.SYMBOL, "1h", "60d")
    df4h = g.to_4h(df1h)
    price = float(df15["Close"].iloc[-1])
    a = float(g.atr(df15).iloc[-1])
    b4, b1 = htf_bias(df4h), htf_bias(df1h)
    rng = df4h.tail(30)
    lo_r, hi_r = float(rng["Low"].min()), float(rng["High"].max())
    pos = (price - lo_r) / max(hi_r - lo_r, 1e-9)
    kz = killzone(ts)
    best = None
    for d in (1, -1):
        frame = df15 if d == 1 else mirror(df15)
        s = detect_long(frame, a)
        if not s or b4 == -d:
            continue
        score = 2
        why = ["سحب سيولة تحت قاع (Sell-side) ثم كسر هيكل صاعد MSS"] if d == 1 \
            else ["سحب سيولة فوق قمة (Buy-side) ثم كسر هيكل هابط MSS"]
        if s["fvg"]:
            score += 1
            why.append("فجوة سعرية FVG")
        if s["ob"]:
            score += 1
            why.append("Order Block")
        if b4 == d:
            score += 1
            why.append("ترند 4H يوافق الاتجاه")
        if b1 == d:
            score += 1
            why.append("ترند 1H يوافق الاتجاه")
        if (pos < 0.5) if d == 1 else (pos > 0.5):
            score += 1
            why.append("منطقة Discount (رخيص)" if d == 1 else "منطقة Premium (غالي)")
        if kz:
            score += 1
            why.append(f"داخل Killzone {kz}")
        zlo, zhi = s["zone"]
        entry = (zlo + zhi) / 2
        sl = s["sweep"] - 0.25 * a
        risk = entry - sl
        if risk <= 0 or risk > 4 * a:
            continue
        tp_liq = float(frame["High"].tail(100).max())
        tp = tp_liq if tp_liq - entry >= 1.5 * risk else entry + 2 * risk
        sg = 1 if d == 1 else -1
        res = {
            "dir": "BUY" if d == 1 else "SELL", "score": score, "reasons": why,
            "entry_lo": min(sg * zlo, sg * zhi), "entry_hi": max(sg * zlo, sg * zhi),
            "sl": sg * sl, "tp": sg * tp, "rr": (tp - entry) / risk,
            "id": f"{d}-{frame.index[s['k']]}", "price": price,
        }
        if score >= ICT_MIN_SCORE and (best is None or score > best["score"]):
            best = res
    return best


def format_message(r):
    icon = "🟢 شراء" if r["dir"] == "BUY" else "🔴 بيع"
    lines = [
        f"<b>{icon} XAUUSD (ICT/SMC)</b>",
        f"السعر الحالي: {r['price']:.2f}",
        f"منطقة الدخول (Limit): <b>{r['entry_lo']:.2f} - {r['entry_hi']:.2f}</b>",
        f"Stop Loss: {r['sl']:.2f}",
        f"Take Profit: {r['tp']:.2f}  (1:{r['rr']:.1f})",
        f"السكور: {r['score']}/8",
        "",
        *[f"• {x}" for x in r["reasons"]],
        "",
        "⏳ الدخول غير بعد رجوع السعر للمنطقة. إلا سد السعر ورا SL الفكرة تلغات.",
        "⚠️ مساعدة في القرار وماشي نصيحة مالية. حدد المخاطرة ديالك.",
    ]
    return "\n".join(lines)


def run_cycle(state):
    news = g.high_impact_news_nearby()
    if news:
        g.log.info("Skipping: high-impact USD news (%s)", news.get("title"))
        return
    r = analyze()
    if r is None:
        g.log.info("No ICT setup (or market closed)")
        return
    g.log.info("ICT setup %s score=%d id=%s", r["dir"], r["score"], r["id"])
    if state.get("last_id") == r["id"] or g.in_cooldown(state, r["dir"]):
        g.log.info("Already sent / cooldown, skipping")
        return
    g.send_telegram(format_message(r))
    state["last"] = {"dir": r["dir"], "time": datetime.now(timezone.utc).isoformat()}
    state["last_id"] = r["id"]
    g.save_state(state)


def main():
    args = set(sys.argv[1:])
    if "--test" in args:
        g.send_telegram("✅ ICT bot خدام وكيتوصل بيك.")
        return
    if "--once" in args:
        print(json.dumps(analyze(), ensure_ascii=False, indent=2, default=str))
        return
    run_cycle(g.load_state())


if __name__ == "__main__":
    main()
