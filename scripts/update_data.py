"""Fetch ~1 year of daily OHLCV for DBS, OCBC and UOB from Yahoo Finance
and write data.json for the chart page.

Run locally:  pip install yfinance pandas && python scripts/update_data.py
GitHub Actions runs this automatically (see .github/workflows/update-data.yml).
"""
import calendar
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import yfinance as yf

SYMBOLS = ["D05.SI", "O39.SI", "U11.SI"]   # DBS, OCBC, UOB
PERIOD = "1y"                               # 1y gives MACD/Stoch plenty of warm-up
OUT = Path(__file__).resolve().parent.parent / "data.json"


def fetch(sym: str):
    df = yf.Ticker(sym).history(period=PERIOD, interval="1d", auto_adjust=False, actions=False)
    if df.empty:
        raise RuntimeError(f"No data returned for {sym}")
    rows = []
    for idx, r in df.iterrows():
        vals = [r["Open"], r["High"], r["Low"], r["Close"], r["Volume"]]
        if any(v is None or (isinstance(v, float) and math.isnan(v)) for v in vals):
            continue
        d = idx.date()  # exchange-local trading date
        ts = calendar.timegm((d.year, d.month, d.day, 0, 0, 0))  # 00:00 UTC of that date
        rows.append([ts, round(float(r["Open"]), 3), round(float(r["High"]), 3),
                     round(float(r["Low"]), 3), round(float(r["Close"]), 3), int(r["Volume"])])
    return rows


def main():
    out = {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "Yahoo Finance (yfinance)",
        "symbols": {},
    }
    for s in SYMBOLS:
        out["symbols"][s] = fetch(s)
        last = out["symbols"][s][-1]
        print(f"{s}: {len(out['symbols'][s])} bars, last {datetime.fromtimestamp(last[0], timezone.utc).date()} close {last[4]}")
    OUT.write_text(json.dumps(out, separators=(",", ":")))
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # keep the old data.json if Yahoo fails
        print(f"Update failed: {e}", file=sys.stderr)
        sys.exit(1)
