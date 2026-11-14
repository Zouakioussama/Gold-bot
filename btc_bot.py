import requests

import gold_bot as g


def btc_fundamental_score():
    score, why = 0, []
    try:
        url = "https://api.alternative.me/fng/?limit=1"
        v = int(requests.get(url, timeout=15).json()["data"][0]["value"])
        if v <= 25:
            score += 1
            why.append(f"خوف شديد في السوق (Fear-Greed={v}) غالبا فرصة ارتداد")
        elif v >= 75:
            score -= 1
            why.append(f"طمع شديد في السوق (Fear-Greed={v}) خطر تصحيح")
    except Exception as exc:
        g.log.warning("Fear-Greed failed: %s", exc)
    try:
        nq = g.fetch("^IXIC", "1d", "1mo")["Close"]
        chg = (nq.iloc[-1] / nq.iloc[-6] - 1) * 100
        if chg > 1.5:
            score += 1
            why.append(f"ناسداك صاعد {chg:+.1f}% / 5 أيام (شهية مخاطرة)")
        elif chg < -1.5:
            score -= 1
            why.append(f"ناسداك نازل {chg:+.1f}% / 5 أيام (هروب من المخاطرة)")
    except Exception as exc:
        g.log.warning("Nasdaq failed: %s", exc)
    return score, why


_orig_format = g.format_message
g.format_message = lambda r: _orig_format(r).replace("XAUUSD", "BTCUSD")
g.fundamental_score = btc_fundamental_score

if __name__ == "__main__":
    g.main()
