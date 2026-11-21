"""Turn broker time & sales exports into daily ask/bid volume (delta.json).

The app's order-flow panel uses delta.json when a day is present and falls
back to an estimate (close position in the day's range) when it is not.

HOW TO USE
  1. Export a day's time & sales from your broker as CSV.
  2. Name it  <CODE>_<YYYY-MM-DD>.csv  e.g.  D05_2026-11-20.csv  (D05 / O39 / U11)
  3. Drop it into the  tns/  folder (GitHub: Add file -> Upload files -> tns/).
  4. The daily GitHub Action runs this script, or run it yourself:
         python scripts/import_tns.py

COLUMNS (header names are matched case-insensitively; first match wins)
  time    : time, trade time, timestamp, datetime
  price   : price, last, trade price, done price
  volume  : volume, vol, qty, quantity, size, shares
  side    : side, type, b/s, buy/sell, aggressor, initiator   (optional)
  bid/ask : bid, ask  (quote at the time of the trade)        (optional)

HOW EACH TRADE IS CLASSIFIED (best available method, per file)
  1. side column  : B/BUY/A/ASK/UP  -> ask (buy);  S/SELL/BID/DN/DOWN -> bid (sell)
  2. bid/ask cols : price >= ask -> ask;  price <= bid -> bid;  between -> unclassified
  3. tick rule    : uptick -> ask, downtick -> bid, same price -> previous side
Trades before 09:00 or from 17:00 SGT (pre-open and closing auctions) are
counted as unclassified, because auction prints have no aggressor.

OUTPUT  delta.json
  {"symbols": {"D05.SI": {"2026-11-20": [ask_vol, bid_vol, unclassified_vol, "side|quote|tick"]}}}
"""
import csv
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TNS = ROOT / "tns"
OUT = ROOT / "delta.json"
CODES = {"D05": "D05.SI", "O39": "O39.SI", "U11": "U11.SI",
         "DBS": "D05.SI", "OCBC": "O39.SI", "UOB": "U11.SI"}
COLS = {
    "time": ["time", "trade time", "timestamp", "datetime", "date/time"],
    "price": ["price", "last", "trade price", "done price", "done"],
    "volume": ["volume", "vol", "qty", "quantity", "size", "shares", "done vol"],
    "side": ["side", "type", "b/s", "buy/sell", "aggressor", "initiator", "trade type"],
    "bid": ["bid", "bid price"],
    "ask": ["ask", "offer", "ask price", "offer price"],
}
BUY = {"b", "buy", "a", "ask", "up", "+", "bought", "buy-up", "offer"}
SELL = {"s", "sell", "bid", "dn", "down", "-", "sold", "sell-down"}


def num(x):
    x = (x or "").replace(",", "").strip()
    if not x:
        return None
    m = re.match(r"^(-?[\d.]+)\s*([kKmM]?)$", x)
    if not m:
        return None
    v = float(m.group(1))
    return v * {"k": 1e3, "m": 1e6}.get(m.group(2).lower(), 1)


def hhmm(t):
    m = re.search(r"(\d{1,2}):(\d{2})", t or "")
    return int(m.group(1)) * 100 + int(m.group(2)) if m else None


def pick(header):
    low = [h.strip().lower() for h in header]
    out = {}
    for key, names in COLS.items():
        for n in names:
            if n in low:
                out[key] = low.index(n)
                break
    return out


def classify(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    if not rows:
        raise ValueError("empty file")
    c = pick(rows[0])
    for k in ("price", "volume"):
        if k not in c:
            raise ValueError(f"no '{k}' column in header {rows[0]}")
    method = "side" if "side" in c else "quote" if {"bid", "ask"} <= c.keys() else "tick"
    trades = []
    for r in rows[1:]:
        if len(r) < len(rows[0]):
            continue
        p, v = num(r[c["price"]]), num(r[c["volume"]])
        if p is None or not v:
            continue
        trades.append((r, p, v, hhmm(r[c["time"]]) if "time" in c else None))
    # tick rule needs chronological order; exports are often newest-first
    if method == "tick" and len(trades) > 1 and "time" in c:
        if (trades[0][3] or 0) > (trades[-1][3] or 0):
            trades.reverse()
    ask = bid = unc = 0.0
    last_p, last_side = None, None
    for r, p, v, t in trades:
        side = None
        if t is not None and (t < 900 or t >= 1700):
            side = "auction"
        elif method == "side":
            s = r[c["side"]].strip().lower()
            side = "a" if s in BUY else "b" if s in SELL else None
        elif method == "quote":
            b, a = num(r[c["bid"]]), num(r[c["ask"]])
            side = "a" if a and p >= a else "b" if b and p <= b else None
        else:
            if last_p is not None:
                side = "a" if p > last_p else "b" if p < last_p else last_side
            last_side = side if side in ("a", "b") else last_side
        if t is None or not (t < 900 or t >= 1700):
            last_p = p
        if side == "a":
            ask += v
        elif side == "b":
            bid += v
        else:
            unc += v
    return round(ask), round(bid), round(unc), method


def main():
    files = sorted(TNS.glob("*.csv")) if TNS.exists() else []
    if not files:
        print("No time & sales files in tns/ - nothing to import.")
        return 0
    data = json.loads(OUT.read_text()) if OUT.exists() else {"symbols": {}}
    data.setdefault("symbols", {})
    done = 0
    for f in files:
        m = re.match(r"^([A-Za-z0-9]+)[_\- ](\d{4}-\d{2}-\d{2})", f.stem)
        if not m or m.group(1).upper() not in CODES:
            print(f"skip {f.name}: name it like D05_2026-11-20.csv")
            continue
        sym, d = CODES[m.group(1).upper()], m.group(2)
        try:
            a, b, u, how = classify(f)
        except Exception as e:
            print(f"skip {f.name}: {e}")
            continue
        data["symbols"].setdefault(sym, {})[d] = [a, b, u, how]
        tot = a + b
        print(f"{sym} {d}: ask {a:,}  bid {b:,}  unclassified {u:,}  "
              f"buy% {100 * a / tot:.1f}  via {how}" if tot else f"{sym} {d}: no classified volume")
        done += 1
    data["updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    data["source"] = "Broker time & sales exports (tns/)"
    for s in data["symbols"]:
        data["symbols"][s] = dict(sorted(data["symbols"][s].items()))
    OUT.write_text(json.dumps(data, indent=1))
    print(f"Imported {done} file(s) -> {OUT.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
