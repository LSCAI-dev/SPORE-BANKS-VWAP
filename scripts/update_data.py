"""Fetch daily OHLCV for DBS, OCBC and UOB and update data.json.

Why not plain yfinance: on some runs yfinance's period="1y" request returned
data that stopped a few sessions short (e.g. ended 13 Nov while Yahoo already
had 16-17 Nov). This version:
  1. calls Yahoo's chart API directly for range=1y AND range=5d, and lets the
     fresher 5-day bars override the 1-year bars by date;
  2. falls back to yfinance with explicit start/end dates if the direct call fails;
  3. merges with the existing data.json so bars are never lost and the file
     never goes backwards in time.

Run locally:  python scripts/update_data.py
"""
import calendar
import json
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

SYMBOLS = ["D05.SI", "O39.SI", "U11.SI"]   # DBS, OCBC, UOB
KEEP_DAYS = 400                             # calendar days of history kept in data.json
OUT = Path(__file__).resolve().parent.parent / "data.json"
HOSTS = ["query1.finance.yahoo.com", "query2.finance.yahoo.com"]
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def day_ts(d):
    """00:00 UTC of a calendar date - the format index.html and scan.py expect."""
    return calendar.timegm((d.year, d.month, d.day, 0, 0, 0))


def chart(sym, rng):
    last_err = None
    for attempt in range(3):
        for host in HOSTS:
            url = f"https://{host}/v8/finance/chart/{sym}?range={rng}&interval=1d&includePrePost=false"
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=20) as r:
                    j = json.load(r)
                res = j["chart"]["result"][0]
                return parse(res)
            except Exception as e:  # try the other host / retry
                last_err = e
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"{sym} {rng}: {last_err}")


def parse(res):
    off = res["meta"].get("gmtoffset", 28800)
    q = res["indicators"]["quote"][0]
    rows = {}
    for i, t in enumerate(res.get("timestamp") or []):
        vals = [q["open"][i], q["high"][i], q["low"][i], q["close"][i], q["volume"][i]]
        if any(v is None for v in vals):
            continue
        d = datetime.fromtimestamp(t + off, timezone.utc).date()  # exchange-local trading date
        rows[day_ts(d)] = [day_ts(d), round(vals[0], 3), round(vals[1], 3), round(vals[2], 3),
                           round(vals[3], 3), int(vals[4])]
    return rows


def via_yfinance(sym):
    import yfinance as yf  # optional fallback
    end = (datetime.now(timezone.utc) + timedelta(days=2)).date()
    start = end - timedelta(days=KEEP_DAYS)
    df = yf.Ticker(sym).history(start=start.isoformat(), end=end.isoformat(), interval="1d",
                                auto_adjust=False, actions=False)
    rows = {}
    for idx, r in df.iterrows():
        v = [r["Open"], r["High"], r["Low"], r["Close"], r["Volume"]]
        if any(x != x for x in v):  # NaN
            continue
        ts = day_ts(idx.date())
        rows[ts] = [ts, round(float(v[0]), 3), round(float(v[1]), 3), round(float(v[2]), 3),
                    round(float(v[3]), 3), int(v[4])]
    return rows


def main():
    old = json.loads(OUT.read_text())["symbols"] if OUT.exists() else {}
    out = {"updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "source": "Yahoo Finance chart API", "symbols": {}}
    cutoff = day_ts((datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).date())
    failures = 0
    for s in SYMBOLS:
        merged = {r[0]: r for r in old.get(s, [])}        # 1) keep what we already have
        try:
            merged.update(chart(s, "1y"))                   # 2) 1-year history
            merged.update(chart(s, "5d"))                   # 3) freshest bars win
            src = "chart API"
        except Exception as e:
            print(f"{s}: direct API failed ({e}); trying yfinance", file=sys.stderr)
            try:
                merged.update(via_yfinance(s)); src = "yfinance"
            except Exception as e2:
                print(f"{s}: yfinance failed too ({e2}); keeping existing bars", file=sys.stderr)
                failures += 1; src = "unchanged"
        rows = sorted((r for r in merged.values() if r[0] >= cutoff), key=lambda r: r[0])
        out["symbols"][s] = rows
        prev_last = old.get(s, [[0]])[-1][0] if old.get(s) else 0
        last = rows[-1]
        print(f"{s}: {len(rows)} bars via {src}; last {datetime.fromtimestamp(last[0], timezone.utc).date()} "
              f"O {last[1]} H {last[2]} L {last[3]} C {last[4]}"
              + ("" if last[0] > prev_last else "  (no new bar)"))
    OUT.write_text(json.dumps(out, separators=(",", ":")))
    print(f"Wrote {OUT}")
    if failures == len(SYMBOLS):
        sys.exit(1)


if __name__ == "__main__":
    main()
