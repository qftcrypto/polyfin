"""Touch-market backtest over every resolved week.

    python -m polyfin.weekly.backtest [--every 2]

1. Proxy fidelity: for each symbol, the offset b (Pyth ~ Yahoo / (1 + b)) that best
   reproduces which strikes were touched, from Yahoo 1h session highs/lows.  Each
   week is scored with b fitted on the OTHER weeks.
2. Every `every` hours of each resolved market's life, while it is untouched (by
   our data and by the market), score the touch model against the market price
   (CLOB history).  A volatility multiplier k (v -> k^2 v) is fitted
   leave-one-week-out.
3. Is the market itself biased?  Market-price bins vs realized touch frequency.
4. Simple entries: first point per market where model - (price +/- half spread)
   - fee >= edge, buying Yes or No.

Same-week strikes of one underlying share one price path, so the effective sample
is the number of underlying-weeks, not markets.
"""
from __future__ import annotations

import argparse
import math
from collections import defaultdict
from datetime import datetime

import numpy as np

from ..data import load_history
from ..db import connect
from ..live.fees import fee_per_share
from ..varclock import ET
from .touch import HourlyVol, prob_touch

B_GRID = np.round(np.arange(-0.03, 0.0301, 0.0005), 4)
K_GRID = np.round(np.arange(0.6, 2.61, 0.05), 2)


def load(conn):
    mk = conn.execute(
        "SELECT condition_id, event_slug, symbol, session, direction, strike, token_yes, "
        "created_ts, end_ts, outcome_yes, closed_ts, volume FROM weekly.markets "
        "WHERE closed = 1 AND outcome_yes IN (0, 1) ORDER BY end_ts").fetchall()
    conn.commit()
    vols = {}
    for sym, session in {(m[2], m[3]) for m in mk}:
        bars = conn.execute("SELECT ts, open, high, low, close FROM weekly.bars_1h "
                            "WHERE symbol = %s ORDER BY ts", (sym,)).fetchall()
        conn.commit()
        vols[sym] = HourlyVol(bars, session)
    return mk, vols


def week_of(m) -> str:
    return datetime.fromtimestamp(m[8], ET).date().isoformat()


def fit_offsets(mk, vols):
    """symbol -> {week: b fitted on the other weeks}, plus agreement stats."""
    rows = defaultdict(list)          # symbol -> (week, direction, strike, outcome, hi, lo)
    for m in mk:
        v = vols[m[2]]
        hi, lo = v.extremes(m[7] or m[8] - 7 * 86400, m[8])
        if hi is None:
            continue
        rows[m[2]].append((week_of(m), m[4], m[5], m[9], hi, lo))

    def agree(rs, b):
        ok = [(hi / (1 + b) >= k) == (y == 1) if d == "up" else (lo / (1 + b) <= k) == (y == 1)
              for _, d, k, y, hi, lo in rs]
        return float(np.mean(ok)) if ok else float("nan")

    out, stats = {}, {}
    for sym, rs in rows.items():
        weeks = sorted({r[0] for r in rs})
        out[sym] = {}
        for w in weeks:
            other = [r for r in rs if r[0] != w] or rs
            out[sym][w] = float(max(B_GRID, key=lambda b: agree(other, b)))
        best = float(max(B_GRID, key=lambda b: agree(rs, b)))
        stats[sym] = (len(rs), len(weeks), agree(rs, 0.0), best, agree(rs, best))
    return out, stats


def points(conn, mk, vols, offs, every_h):
    """(week, symbol, session, cid, tau_h, S, H, direction, v, market_p, y, volume)."""
    pts = []
    for m in mk:
        cid, ev, sym, session, d, H, tok, created, end, y, closed_ts, vol = m
        w = week_of(m)
        b = offs.get(sym, {}).get(w)
        hv = vols[sym]
        if b is None or not created:
            continue
        hist = load_history(conn, tok)
        t = created + 3600
        stop = min(end, closed_ts or end)
        while t < stop - 1800:
            S = hv.last_close(t)
            v = hv.var(t, end, session)
            p = hist.at(t, max_age=3600)
            if S and v is not None and p is not None:
                S /= (1 + b)
                hi, lo = hv.extremes(created, t)
                touched = hi is not None and (hi / (1 + b) >= H if d == "up" else lo / (1 + b) <= H)
                if not touched and 0.005 < p < 0.995:
                    pts.append((w, sym, session, cid, (end - t) / 3600, S, H, d, v, p, y, vol))
            t += every_h * 3600
    return pts


def model_p(pts, k):
    return np.array([prob_touch(x[7], x[5], x[6], k * k * x[8]) for x in pts])


def ll(p, y):
    p = np.clip(p, 1e-3, 1 - 1e-3)
    return -(y * np.log(p) + (1 - y) * np.log(1 - p))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--every", type=float, default=2.0, help="hours between scored points")
    args = ap.parse_args()
    conn = connect()
    mk, vols = load(conn)
    weeks = sorted({week_of(m) for m in mk})
    print(f"{len(mk)} resolved touch markets, {len({(m[1]) for m in mk})} underlying-weeks, "
          f"{len(weeks)} weeks ({weeks[0]} .. {weeks[-1]})")

    offs, stats = fit_offsets(mk, vols)
    print("\n== 1. proxy fidelity: does the Yahoo session high/low reproduce which strikes "
          "were touched?")
    print(f"   {'symbol':9} {'mkts':>5} {'weeks':>5} {'agree b=0':>9} {'best b':>7} {'agree':>6}")
    for sym, (n, nw, a0, b, ab) in sorted(stats.items()):
        print(f"   {sym:9} {n:5d} {nw:5d} {100 * a0:8.1f}% {100 * b:+6.2f}% {100 * ab:5.1f}%")

    pts = points(conn, mk, vols, offs, args.every)
    y = np.array([x[10] for x in pts], dtype=float)
    mkt = np.array([x[9] for x in pts])
    wk = np.array([x[0] for x in pts])
    tau = np.array([x[4] for x in pts])
    print(f"\n{len(pts)} scored points on {len({x[3] for x in pts})} markets "
          f"({len({(x[0], x[1]) for x in pts})} underlying-weeks)")

    # leave-one-week-out k
    p_cv, ks = np.zeros(len(pts)), {}
    for w in sorted(set(wk)):
        tr = wk != w
        trp = [x for x, t in zip(pts, tr) if t]
        k = min(K_GRID, key=lambda k: ll(model_p(trp, k), y[tr]).mean())
        ks[w] = k
        p_cv[~tr] = model_p([x for x, t in zip(pts, ~tr) if t], k)
    p1 = model_p(pts, 1.0)
    print(f"   volatility multiplier k (leave-one-week-out): median {np.median(list(ks.values())):.2f}, "
          f"range {min(ks.values()):.2f}-{max(ks.values()):.2f}")

    print("\n== 2. scores (lower is better)")
    print(f"   {'':18} {'n':>6} {'brier k=1':>9} {'brier k_cv':>10} {'brier mkt':>9} "
          f"{'ll k=1':>7} {'ll k_cv':>7} {'ll mkt':>7}")
    groups = [("all", np.ones(len(y), bool))]
    groups += [(f"session {s}", np.array([x[2] == s for x in pts])) for s in ("rth", "cme", "fx")]
    groups += [(f"{lo}-{hi}h left", (tau >= lo) & (tau < hi))
               for lo, hi in [(0, 24), (24, 72), (72, 200)]]
    for name, g in groups:
        if g.sum():
            f = lambda p: ((p[g] - y[g]) ** 2).mean()
            print(f"   {name:18} {g.sum():6d} {f(p1):9.4f} {f(p_cv):10.4f} {f(mkt):9.4f} "
                  f"{ll(p1[g], y[g]).mean():7.3f} {ll(p_cv[g], y[g]).mean():7.3f} "
                  f"{ll(mkt[g], y[g]).mean():7.3f}")

    print("\n== 3. calibration: predicted vs realized touch frequency")
    print(f"   {'bin':10} {'n':>6} {'model k_cv':>10} {'freq':>6} | {'n':>6} {'market':>7} {'freq':>6}")
    edges = [0, 0.02, 0.05, 0.1, 0.2, 0.35, 0.5, 0.65, 0.8, 0.9, 1.01]
    for lo, hi in zip(edges, edges[1:]):
        a = (p_cv >= lo) & (p_cv < hi)
        b = (mkt >= lo) & (mkt < hi)
        fm = lambda s: f"{y[s].mean():6.3f}" if s.sum() else "     -"
        print(f"   {lo:.2f}-{min(hi, 1):.2f} {a.sum():6d} {p_cv[a].mean() if a.sum() else 0:10.3f} {fm(a)} | "
              f"{b.sum():6d} {mkt[b].mean() if b.sum() else 0:7.3f} {fm(b)}")

    print("\n== 4. simple entries: first point per market, price = market +/- 0.02 (half spread)")
    print(f"   {'rule':28} {'trades':>6} {'u-weeks':>7} {'win%':>5} {'ret/$':>7} {'total$':>7} "
          f"{'weeks+':>7}")
    for side_rule in ("both", "no only", "yes only"):
        for th in (0.05, 0.10):
            seen, rets, uw, byw = set(), [], set(), defaultdict(float)
            for i, x in enumerate(pts):
                if x[3] in seen:
                    continue
                cands = []
                if side_rule != "no only":
                    a_ = min(0.99, x[9] + 0.02)
                    cands.append((p_cv[i] - a_ - fee_per_share(a_), a_, x[10]))
                if side_rule != "yes only":
                    a_ = min(0.99, 1 - x[9] + 0.02)
                    cands.append((1 - p_cv[i] - a_ - fee_per_share(a_), a_, 1 - x[10]))
                e, a_, win = max(cands)
                if e >= th and 0.03 <= a_ <= 0.97:
                    seen.add(x[3])
                    r = win / (a_ + fee_per_share(a_)) - 1
                    rets.append((r, win))
                    uw.add((x[0], x[1]))
                    byw[x[0]] += r
            if rets:
                r = np.array([q[0] for q in rets])
                print(f"   {side_rule + f', edge >= {th:.2f}':28} {len(r):6d} {len(uw):7d} "
                      f"{100 * np.mean([q[1] for q in rets]):5.0f} {100 * r.mean():+6.1f}% "
                      f"{r.sum():+7.2f} {sum(v > 0 for v in byw.values()):3d}/{len(byw):<3d}")


if __name__ == "__main__":
    main()
