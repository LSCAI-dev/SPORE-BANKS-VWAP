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


# ---------- Setup C "Claude for SGX banks" (identical to setupC / setupCTrades in index.html) ----------
def sma_run(a, n):
    out, t = [], 0.0
    for i, v in enumerate(a):
        t += v
        if i >= n:
            t -= a[i - n]
        out.append(t / n if i >= n - 1 else None)
    return out


def adx14(B, n=14):
    L = len(B)
    pdi, mdi, adx = [None] * L, [None] * L, [None] * L
    sTR = sP = sM = 0.0
    dxs, a = [], None
    for i in range(1, L):
        h, l, pc = B[i]["high"], B[i]["low"], B[i - 1]["close"]
        up, dn = h - B[i - 1]["high"], B[i - 1]["low"] - l
        tr = max(h - l, abs(h - pc), abs(l - pc))
        pdm = up if up > dn and up > 0 else 0
        mdm = dn if dn > up and dn > 0 else 0
        if i <= n:
            sTR += tr; sP += pdm; sM += mdm
            if i < n:
                continue
        else:
            sTR = sTR - sTR / n + tr; sP = sP - sP / n + pdm; sM = sM - sM / n + mdm
        P = 100 * sP / sTR if sTR else 0
        M = 100 * sM / sTR if sTR else 0
        dx = 100 * abs(P - M) / (P + M) if (P + M) else 0
        pdi[i], mdi[i] = P, M
        if a is None:
            dxs.append(dx)
            if len(dxs) == n:
                a = sum(dxs) / n; adx[i] = a
        else:
            a = (a * (n - 1) + dx) / n; adx[i] = a
    return adx, pdi, mdi


def atr14(B, n=14):
    out, v, s_ = [None] * len(B), None, 0.0
    for i in range(1, len(B)):
        tr = max(B[i]["high"] - B[i]["low"], abs(B[i]["high"] - B[i - 1]["close"]), abs(B[i]["low"] - B[i - 1]["close"]))
        if i <= n:
            s_ += tr
            if i == n:
                v = s_ / n; out[i] = v
        else:
            v = (v * (n - 1) + tr) / n; out[i] = v
    return out


def setup_c(B, I):
    c = [b["close"] for b in B]; v = [b["volume"] for b in B]; L = len(B)
    e8, e21 = ema(c, 8), ema(c, 21)
    v5, v20 = sma_run(v, 5), sma_run(v, 20)
    adx, pdi, mdi = adx14(B)
    obv = [0.0]
    for i in range(1, L):
        obv.append(obv[-1] + (v[i] if c[i] > c[i - 1] else -v[i] if c[i] < c[i - 1] else 0))
    obv_hi = [max(obv[i - 20:i]) if i >= 20 else None for i in range(L)]
    udr = []
    for i in range(L):
        if i < 20:
            udr.append(None); continue
        u = d = 0.0
        for j in range(i - 19, i + 1):
            if c[j] > c[j - 1]: u += v[j]
            elif c[j] < c[j - 1]: d += v[j]
        udr.append(u / d if d else (99 if u else 1))
    atr = atr14(B)
    hst = [m["h"] if m else None for m in I["M"]]

    def at(i):
        k = {"adx_ok": adx[i] is not None and adx[i] > 20 and pdi[i] > mdi[i],
             "ema_ok": e8[i] > e21[i],
             "vol_ok": v5[i] is not None and v20[i] is not None and v5[i] > v20[i],
             "udr_ok": udr[i] is not None and udr[i] > 1.3,
             "obv_ok": obv_hi[i] is not None and obv[i] >= obv_hi[i]}
        k["n"] = sum(1 for x in k.values() if x)
        k["long"] = k["n"] == 5
        return k

    def down3(i):
        return i >= 3 and all(hst[i - k] is not None and hst[i - k - 1] is not None and hst[i - k] < hst[i - k - 1] for k in range(3))

    # walk-forward position (entry at signal close; exits: EMA 8 half, EMA 21 all, 1.5 ATR stop -> 2 ATR trail, MACD tighten)
    state = [None] * L
    hold, hh, tr, mult, entry = 0, 0.0, 0.0, 2, None
    for i in range(21, L):
        r = B[i]; ev = []
        if not hold:
            if atr[i] is not None and at(i)["long"]:
                hold, hh, mult = 100, r["high"], 2
                tr = r["close"] - 1.5 * atr[i]; entry = {"i": i, "px": r["close"], "stop": tr}
                ev.append({"t": "buy", "px": r2(r["close"]), "why": "Setup C LONG"})
        elif r["low"] <= tr:
            ev.append({"t": "exit", "px": r2(min(r["open"], tr)), "why": "Initial 1.5 x ATR stop hit" if tr == entry["stop"] else f"{mult} x ATR trail hit"})
            hold = 0
        elif r["close"] < e21[i]:
            ev.append({"t": "exit", "px": r2(r["close"]), "why": "Close < EMA 21"}); hold = 0
        else:
            if hold == 100 and r["close"] < e8[i]:
                hold = 50; ev.append({"t": "half", "px": r2(r["close"]), "why": "Close < EMA 8"})
            if mult == 2 and down3(i):
                mult = 1; ev.append({"t": "tighten", "px": None, "why": "MACD histogram down 3 bars"})
            hh = max(hh, r["high"]); tr = max(tr, hh - mult * atr[i])
        state[i] = {"hold": hold, "stop": r2(tr) if hold else None, "mult": mult if hold else None,
                    "entry_date": B[entry["i"]]["time"] if hold and entry else None, "events": ev}

    def snap(i):
        k = at(i)
        cross = None
        if i >= 1:
            a_, b_ = e8[i] - e21[i], e8[i - 1] - e21[i - 1]
            cross = "bull" if a_ > 0 and b_ <= 0 else "bear" if a_ < 0 and b_ >= 0 else None
        rnd = lambda x, d=2: None if x is None else round(x, d)
        return {**k, "adx": rnd(adx[i]), "pdi": rnd(pdi[i]), "mdi": rnd(mdi[i]), "e8": rnd(e8[i], 3), "e21": rnd(e21[i], 3),
                "v5": None if v5[i] is None else int(v5[i]), "v20": None if v20[i] is None else int(v20[i]),
                "udr": rnd(udr[i]), "obv": int(obv[i]), "obv_hi": None if obv_hi[i] is None else int(obv_hi[i]),
                "atr": rnd(atr[i], 3), "cross": cross, "pos": state[i]}
    return snap


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
    added = filled = c_added = 0

    for bank in cfg:
        sym = bank["sym"]
        if sym not in data:
            continue
        B = [{"time": iso(r[0]), "open": r[1], "high": r[2], "low": r[3], "close": r[4], "volume": r[5]} for r in data[sym]]
        I = indicators(B)
        snapC = setup_c(B, I)
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
                   "scanned_at": now.strftime("%Y-%m-%d %H:%M SGT"), **ev, "c": snapC(i), "next": None, "outcome": None}
            log["scans"].append(rec); by_key[key] = rec; added += 1

        # 2) fill next-day prices once (only empty slots, only final bars)
        idx = {b["time"]: i for i, b in enumerate(B)}
        # 2b) Setup C added once to older records that pre-date it (no other field is touched)
        for (date, s_sym), rec in by_key.items():
            if s_sym == sym and "c" not in rec and date in idx:
                rec["c"] = snapC(idx[date]); c_added += 1
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
    print(f"scans.json: {len(log['scans'])} records ({added} added, {filled} next-day fills, {c_added} Setup C fills)")


def c_cols(c):
    if not c:
        return [""] * 17
    p = c.get("pos") or {}
    evs = "; ".join(f"{e['t']}{' @' + str(e['px']) if e.get('px') is not None else ''} ({e['why']})" for e in p.get("events", []))
    return [f"{c['n']}/5", "Y" if c["long"] else "N", c["adx"], c["pdi"], c["mdi"], c["e8"], c["e21"], c["v5"], c["v20"],
            c["udr"], c["obv"], c["obv_hi"], c["atr"], c["cross"] or "", p.get("hold", 0) if p else 0, p.get("stop") or "", evs]


def write_csv(scans):
    cols = ["date", "nm", "sym", "status", "location_avwap", "momentum_macd", "timing_stoch", "close", "avwap", "dist_pct",
            "minus1sd", "minus2sd", "plus1sd", "plus2sd", "macd_hist", "k", "d", "macd_cross_close", "swing_low", "swing_high",
            "anchor", "A_entry", "A_stop", "A_t1", "A_r1", "A_t2", "A_r2", "B_entry", "B_stop", "B_t1", "B_r1", "B_t2", "B_r2",
            "next_date", "next_open", "next_high", "next_low", "next_close", "next_chg_pct",
            "A_triggered", "A_stop_touched", "A_t1_touched", "B_invalidated", "B_stop_touched", "B_t1_touched",
            "C_score", "C_long", "C_adx", "C_pdi", "C_mdi", "C_ema8", "C_ema21", "C_vol5", "C_vol20", "C_updown", "C_obv",
            "C_obv_prev20_high", "C_atr", "C_cross", "C_holding_pct", "C_stop", "C_events", "source"]
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
                        o("B", "invalidated"), o("B", "stop_touched"), o("B", "t1_touched"),
                        *c_cols(s.get("c")), s["source"]])


if __name__ == "__main__":
    main()
