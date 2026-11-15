"""Daily scan log for the SG banks app.

Reads data.json + config.json, applies the same trading rules as index.html,
and maintains:
  scans.json     - permanent scan history (one record per bank per trading day)
  scan_log.csv   - flat copy of scans.json for Excel (fully regenerated each run)

No repeat saving:
  * A scan is keyed by (date, symbol). If the key already exists it is never
    re-saved or overwritten.
  * The next-day O/H/L/C is filled in once, only while it is still empty.
  * A bar is only scanned / used as next-day data once it is final
    (an earlier date, or today after 17:30 SGT).

Usage:
  python scripts/scan.py                 # normal daily run (GitHub Actions)
  python scripts/scan.py --backfill 60   # also rebuild the last 60 trading days
"""
import argparse
import csv
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA, CONFIG = ROOT / "data.json", ROOT / "config.json"
SCANS, CSV_OUT = ROOT / "scans.json", ROOT / "scan_log.csv"
SGT = timezone(timedelta(hours=8))
FINAL_AFTER = (17, 30)  # SGX closes 17:00; bars after 17:30 SGT are treated as final


def r2(x):
    """Round half up to 2 dp (matches JavaScript Math.round(x*100)/100)."""
    return math.floor(x * 100 + 0.5) / 100


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).date().isoformat()


# ---------- indicators (identical to index.html) ----------
def ema(a, n):
    k, out, e = 2 / (n + 1), [], None
    for i, v in enumerate(a):
        e = v if i == 0 else v * k + e * (1 - k)
        out.append(e)
    return out


def sma(a, n):
    out = []
    for i in range(len(a)):
        w = a[i - n + 1:i + 1] if i >= n - 1 else None
        out.append(None if w is None or any(x is None for x in w) else sum(w) / n)
    return out


def indicators(B):
    c = [b["close"] for b in B]
    e12, e26 = ema(c, 12), ema(c, 26)
    m = [a - b for a, b in zip(e12, e26)]
    sg = ema(m, 9)
    M = [None if i < 25 else {"m": m[i], "s": sg[i], "h": m[i] - sg[i]} for i in range(len(B))]
    raw = []
    for i in range(len(B)):
        if i < 13:
            raw.append(None)
            continue
        hh = max(b["high"] for b in B[i - 13:i + 1])
        ll = min(b["low"] for b in B[i - 13:i + 1])
        raw.append(50.0 if hh == ll else 100 * (B[i]["close"] - ll) / (hh - ll))
    k = sma(raw, 3)
    d = sma(k, 3)
    return {"M": M, "K": k, "D": d, "e12": e12, "e26": e26, "sg": sg}


def avwap(B, anchor):
    pv = v = pv2 = 0.0
    out = []
    for b in B:
        if b["time"] < anchor:
            out.append(None)
            continue
        tp = (b["high"] + b["low"] + b["close"]) / 3
        pv += tp * b["volume"]; v += b["volume"]; pv2 += tp * tp * b["volume"]
        vw = pv / v
        out.append({"vw": vw, "sd": math.sqrt(max(pv2 / v - vw * vw, 0))})
    return out


# ---------- rules (identical to evalRules in index.html) ----------
def evaluate(B, I, A, i):
    L, a, m, mp = B[i], A[i], I["M"][i], I["M"][i - 1]
    k, d = I["K"][i], I["D"][i]
    if a is None or m is None or mp is None or k is None or d is None:
        return None
    first = next(j for j, x in enumerate(A) if x)
    n_anc = i - first + 1
    sw = B[max(0, i - 14):i + 1]
    sw_low, sw_high = min(b["low"] for b in sw), max(b["high"] for b in sw)
    k12, k26 = 2 / 13, 2 / 27
    cx = (I["sg"][i] - I["e12"][i] * (1 - k12) + I["e26"][i] * (1 - k26)) / (k12 - k26)
    kmin = min((x if x is not None else 0) for x in I["K"][i - 4:i + 1])
    vw, sd = a["vw"], a["sd"]
    c1 = L["close"] > vw
    c2 = m["h"] > 0 and m["h"] > mp["h"]
    c3 = k > d and k < 80 and kmin < 50
    buf = lambda p: max(0.01, r2(p * 0.003))
    status = "ENTRY" if (c1 and c2 and c3) else ("STAND ASIDE" if (not c1 and m["h"] < 0) else "WAIT")

    setups = []
    # Setup A - trend (AVWAP)
    entry = L["high"] if c1 else vw
    imm = n_anc < 10
    sup = ([sw_low] if imm else [vw - sd, sw_low]) + ([vw] if c1 and not imm else [])
    sup = [x for x in sup if x < entry] or [sw_low]
    ns = max(sup)
    stop = ns - buf(ns)
    t1, t2 = vw + sd, vw + 2 * sd
    if t1 <= entry:
        t1 = t2; t2 = max(sw_high, t1)
    if t2 <= t1:
        t2 = max(sw_high, t1)
    if imm:
        t2 = max(t2, sw_high)
    setups.append({"id": "A", "trigger": "break_high" if c1 else "close_above",
                   "entry": entry, "stop": stop, "t1": t1, "t2": t2})
    # Setup B - mean reversion (only at/below -1 sigma)
    if L["close"] <= vw - sd:
        setups.append({"id": "B", "trigger": "macd_confirm", "entry": L["close"],
                       "stop": sw_low - buf(sw_low), "t1": vw - sd, "t2": vw, "inval": sw_low})
    for s in setups:
        s["risk"] = s["entry"] - s["stop"]
        s["r1"] = (s["t1"] - s["entry"]) / s["risk"] if s["risk"] > 0 else None
        s["r2"] = (s["t2"] - s["entry"]) / s["risk"] if s["risk"] > 0 else None
        for key in ("entry", "stop", "t1", "t2", "risk", "inval"):
            if key in s:
                s[key] = r2(s[key])
        for key in ("r1", "r2"):
            if s[key] is not None:
                s[key] = round(s[key], 2)

    return {
        "status": status, "c1": c1, "c2": c2, "c3": c3,
        "close": r2(L["close"]), "open": r2(L["open"]), "high": r2(L["high"]), "low": r2(L["low"]),
        "volume": L["volume"],
        "avwap": r2(vw), "sd": round(sd, 4),
        "bands": {"m2": r2(vw - 2 * sd), "m1": r2(vw - sd), "p1": r2(vw + sd), "p2": r2(vw + 2 * sd)}, "dist_pct": round((L["close"] / vw - 1) * 100, 2),
        "macd": round(m["m"], 4), "signal": round(m["s"], 4), "hist": round(m["h"], 4),
        "k": round(k, 2), "d": round(d, 2), "macd_cross_close": r2(cx),
        "swing_low": r2(sw_low), "swing_high": r2(sw_high), "bars_since_anchor": n_anc,
        "setups": setups,
    }


def outcome(scan, nb):
    """Grade a scan against the next day's bar (no look-ahead beyond that day)."""
    res = {}
    for s in scan["setups"]:
        o = {}
        if s["trigger"] in ("break_high", "close_above"):
            o["triggered"] = nb["high"] > s["entry"] if s["trigger"] == "break_high" else nb["close"] > s["entry"]
            if o["triggered"]:  # stop / target only matter once the trade is on
                o["stop_touched"] = nb["low"] <= s["stop"]
                o["t1_touched"] = nb["high"] >= s["t1"]
        else:  # Setup B: entry needs MACD confirmation; record invalidation and what a close entry would have seen
            o["invalidated"] = nb["close"] < s["inval"]
            o["stop_touched"] = nb["low"] <= s["stop"]
            o["t1_touched"] = nb["high"] >= s["t1"]
        res[s["id"]] = o
    return res


# ---------- main ----------
def bar_is_final(date_str, now_sgt):
    today = now_sgt.date().isoformat()
    return date_str < today or (date_str == today and (now_sgt.hour, now_sgt.minute) >= FINAL_AFTER)


def anchor_for(anchors, date_str):
    past = [a for a in anchors if a["date"] <= date_str]
    return past[-1] if past else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", type=int, default=0, help="rebuild the last N trading days (existing records are kept)")
    args = ap.parse_args()

    now = datetime.now(SGT)
    data = json.loads(DATA.read_text())["symbols"]
    cfg = json.loads(CONFIG.read_text())["banks"]
    log = json.loads(SCANS.read_text()) if SCANS.exists() else {"scans": []}
    by_key = {(s["date"], s["sym"]): s for s in log["scans"]}
    added = filled = 0

    for bank in cfg:
        sym = bank["sym"]
        if sym not in data:
            continue
        B = [{"time": iso(r[0]), "open": r[1], "high": r[2], "low": r[3], "close": r[4], "volume": r[5]} for r in data[sym]]
        I = indicators(B)
        final_idx = [i for i, b in enumerate(B) if bar_is_final(b["time"], now)]
        if not final_idx:
            continue
        last = final_idx[-1]
        # normal run: catch up on any of the last 5 final bars missing from the log (e.g. a missed run)
        span = args.backfill if args.backfill else 5
        targets = range(max(30, last - span + 1), last + 1)
        av_cache = {}

        # 1) add new scans (never re-save an existing date)
        for i in targets:
            key = (B[i]["time"], sym)
            if key in by_key:
                continue
            anc = anchor_for(bank["anchors"], B[i]["time"])
            if not anc:
                continue
            A = av_cache.setdefault(anc["date"], avwap(B, anc["date"]))
            ev = evaluate(B, I, A, i)
            if not ev:
                continue
            rec = {"date": B[i]["time"], "sym": sym, "nm": bank["nm"], "anchor": anc["date"],
                   "source": "backfill" if args.backfill else ("live" if i == last else "catch-up"),
                   "scanned_at": now.strftime("%Y-%m-%d %H:%M SGT"), **ev, "next": None, "outcome": None}
            log["scans"].append(rec); by_key[key] = rec; added += 1

        # 2) fill next-day prices once (only empty slots, only final bars)
        idx = {b["time"]: i for i, b in enumerate(B)}
        for (date, s_sym), rec in by_key.items():
            if s_sym != sym or rec.get("next") or date not in idx:
                continue
            j = idx[date] + 1
            if j < len(B) and bar_is_final(B[j]["time"], now):
                nb = B[j]
                rec["next"] = {"date": nb["time"], "open": r2(nb["open"]), "high": r2(nb["high"]),
                               "low": r2(nb["low"]), "close": r2(nb["close"]), "volume": nb["volume"],
                               "chg_pct": round((nb["close"] / rec["close"] - 1) * 100, 2)}
                rec["outcome"] = outcome(rec, nb)
                filled += 1

    log["scans"].sort(key=lambda s: (s["date"], s["sym"]))
    log["updated"] = now.strftime("%Y-%m-%dT%H:%M:%S+08:00")
    SCANS.write_text(json.dumps(log, separators=(",", ":")))
    write_csv(log["scans"])
    print(f"scans.json: {len(log['scans'])} records ({added} added, {filled} next-day fills)")


def write_csv(scans):
    cols = ["date", "nm", "sym", "status", "location_avwap", "momentum_macd", "timing_stoch", "close", "avwap", "dist_pct",
            "minus1sd", "minus2sd", "plus1sd", "plus2sd", "macd_hist", "k", "d", "macd_cross_close", "swing_low", "swing_high",
            "anchor", "A_entry", "A_stop", "A_t1", "A_r1", "A_t2", "A_r2", "B_entry", "B_stop", "B_t1", "B_r1", "B_t2", "B_r2",
            "next_date", "next_open", "next_high", "next_low", "next_close", "next_chg_pct",
            "A_triggered", "A_stop_touched", "A_t1_touched", "B_invalidated", "B_stop_touched", "B_t1_touched", "source"]
    with CSV_OUT.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for s in sorted(scans, key=lambda x: (x["date"], x["sym"]), reverse=True):
            st = {x["id"]: x for x in s["setups"]}
            nx, oc = s.get("next") or {}, s.get("outcome") or {}
            g = lambda sid, k: st.get(sid, {}).get(k, "")
            o = lambda sid, k: "" if sid not in oc or k not in oc[sid] else ("Y" if oc[sid][k] else "N")
            w.writerow([s["date"], s["nm"], s["sym"], s["status"], "Y" if s["c1"] else "N", "Y" if s["c2"] else "N",
                        "Y" if s["c3"] else "N", s["close"], s["avwap"], s["dist_pct"],
                        s["bands"]["m1"], s["bands"]["m2"], s["bands"]["p1"], s["bands"]["p2"],
                        s["hist"], s["k"], s["d"], s["macd_cross_close"], s["swing_low"], s["swing_high"], s["anchor"],
                        g("A", "entry"), g("A", "stop"), g("A", "t1"), g("A", "r1"), g("A", "t2"), g("A", "r2"),
                        g("B", "entry"), g("B", "stop"), g("B", "t1"), g("B", "r1"), g("B", "t2"), g("B", "r2"),
                        nx.get("date", ""), nx.get("open", ""), nx.get("high", ""), nx.get("low", ""), nx.get("close", ""),
                        nx.get("chg_pct", ""), o("A", "triggered"), o("A", "stop_touched"), o("A", "t1_touched"),
                        o("B", "invalidated"), o("B", "stop_touched"), o("B", "t1_touched"), s["source"]])


if __name__ == "__main__":
    main()
