"""Strikes: how deep are the books where strike2 fires, and how far it scales.

    .venv/bin/python -m research.strike_depth

Same points as research/strike_repeat.py (strikes, strikes-sharpened model, LODO,
every minute matched to a recorded book <= 60s old).  The strike2 rule (2 buys at
edge >= 0.05, >= 15 min apart, same side) with $S per buy, the order walking the
recorded ask ladder up to a limit:
  cap1c  min(model limit, best ask + 1c)   - live today
  model  the model limit only: the highest whole-cent price still leaving edge >= 0.05
The recorded book never shows our own fills, and nobody reacts to us here: other
traders taking the same asks, or repricing after a large buy, would cut both fill
and edge.  Treat every number as an upper bound.
"""
from __future__ import annotations

import math
from bisect import bisect_right
from collections import defaultdict

import numpy as np

from polyfin.db import connect
from polyfin.live.fees import fee_per_share
from polyfin.live.sizing import exec_limit, limit_price
from polyfin.stage1 import load_specs
from polyfin.stage2 import Stage2, collect, evaluate

from .strike_repeat import load_books

SIZES = (3, 10, 25, 50, 100, 250, 500)        # 3 = the live rule ($3, >= 5 shares, <= $5)


def depth_usd(lad, limit):
    return sum(p * z for p, z in lad if p <= limit + 1e-9)


def edge_of(q, ask):
    return q - ask - fee_per_share(ask)


def walk_usd(lad, limit, usd):
    """Buy up to `usd` (before fee) walking the ladder to `limit`:
    (shares, avg price, [(price, shares taken)])."""
    left, sh, cost, used = usd, 0.0, 0.0, []
    for p, z in lad:
        if p > limit + 1e-9 or left < p:
            break
        q = min(z, math.floor(left / p))
        if q <= 0:
            break
        sh += q
        cost += q * p
        left -= q * p
        used.append((p, q))
    return sh, (cost / sh if sh else None), used


def run(points, size, mode, max_buys=2, refill=True):
    """refill=False: what we bought at each price stays gone for this market's later
    buys (the recorded book never shows our fills) - the conservative bound."""
    fills, wanted = [], 0.0
    for cid, pts in points.items():
        side, last_t, last_book, n = None, -1e18, None, 0
        taken = defaultdict(float)                 # price -> shares we took (this market/side)
        for t, tau, day, py, bts, ya, na, y, series in pts:
            if tau <= 3 or n >= max_buys:
                break
            sides = {"yes": (py, ya, y), "no": (1 - py, na, 1 - y)}

            def edge(s):
                q, lad, _ = sides[s]
                if not lad or not 0.05 <= lad[0][0] <= 0.97:
                    return None
                return q - lad[0][0] - fee_per_share(lad[0][0])
            if side is None:
                c = [(edge(s), s) for s in sides if edge(s) is not None]
                if not c or max(c)[0] < 0.05:
                    continue
                pick = max(c)[1]
            else:
                pick = side
            e = edge(pick)
            if e is None or e < 0.05 or t - last_t < 900 or bts == last_book:
                continue
            q, lad, win = sides[pick]
            if not refill:
                lad = [(px, z - taken[px]) for px, z in lad if z - taken[px] > 1e-9]
                if not lad or edge_of(q, lad[0][0]) < 0.05:
                    continue
            lim = exec_limit(q, 0.05, lad[0][0]) if mode == "cap1c" else limit_price(q, 0.05)
            if lim is None:
                continue
            usd = size if size != 3 else min(max(3.0, 5 * lim), 5.0)   # 3 = live sizing: >= 5 sh, <= $5
            sh, avg, used = walk_usd(lad, lim, usd)
            if sh >= 5:
                wanted += usd
                side, last_t, last_book, n = pick, t, bts, n + 1
                fills.append((cid, day, series, sh, avg, sh * (avg + fee_per_share(avg)), win))
                for px, z in used:
                    taken[px] += z
    return fills, wanted


def main() -> None:
    conn = connect()
    specs = [s for s in load_specs(conn) if s.kind == "strikes"]
    sp = {s.condition_id: s for s in specs}
    a = collect(Stage2(conn), specs, conn, step=1, hours=36)
    out, _ = evaluate(a)
    p = out["sharp<=3h"]
    lad = load_books(conn, list({sp[c].token_yes for c in set(a["cid"])}))
    points = defaultdict(list)
    for i in range(len(a["y"])):
        s = sp[a["cid"][i]]
        t = s.target_ts - a["tau"][i] * 3600
        ts, v = lad.get(s.token_yes, ([], []))
        j = bisect_right(ts, t) - 1
        if j >= 0 and t - ts[j] <= 60:
            points[s.condition_id].append((t, a["tau"][i], a["day"][i], p[i], ts[j], v[j][0], v[j][1],
                                           a["y"][i], s.series_slug.split("-")[0]))
    for c in points:
        points[c].sort()
    days = sorted({q[2] for pts in points.values() for q in pts})
    nd = len(days)
    print(f"strikes: {len(points)} resolved markets with books, {nd} days ({days[0]}..{days[-1]})\n")

    # 1. depth at the moments strike2 fires (first sight of each buy, $3 rule)
    print("== 1. book depth on our side at the moments strike2 buys ($ offered, before fee)")
    depth = defaultdict(list)
    for cid, pts in points.items():
        side, last_t, n = None, -1e18, 0
        for t, tau, day, py, bts, ya, na, y, series in pts:
            if tau <= 3 or n >= 2:
                break
            for s_, q, l in (("yes", py, ya), ("no", 1 - py, na)):
                if side not in (None, s_) or not l or not 0.05 <= l[0][0] <= 0.97:
                    continue
                if q - l[0][0] - fee_per_share(l[0][0]) >= 0.05 and t - last_t >= 900:
                    ml = limit_price(q, 0.05)
                    depth[series].append((depth_usd(l, l[0][0]), depth_usd(l, l[0][0] + 0.01),
                                          depth_usd(l, ml) if ml else 0.0))
                    side, last_t, n = s_, t, n + 1
                    break
    print(f"   {'series':7} {'buys':>5} | {'at best ask':>22} | {'within ask+1c':>22} | {'up to model limit':>22}")
    print(f"   {'':7} {'':>5} | {'p25    median    p75':>22} | {'p25    median    p75':>22} | {'p25    median    p75':>22}")
    for s_ in sorted(depth):
        d = np.array(depth[s_])
        f = lambda col: "  ".join(f"${x:6.0f}" for x in np.percentile(d[:, col], [25, 50, 75]))
        print(f"   {s_:7} {len(d):5d} | {f(0)} | {f(1)} | {f(2)}")

    # 2. scaling
    hdr = (f"   {'variant':16} {'$/buy':>6} {'buys':>5} {'filled $':>9} {'fill%':>6} {'avg px':>7} {'ret/$':>7} "
           f"{'pnl/day':>9} {'cap/day':>8} {'days+':>6} {'worst day':>10} {'w/o best5':>10}")
    runs = [("2. strike2 (2 buys/market) at larger sizes per buy",
             [("cap1c", "ask+1c", 2, True), ("model", "model limit", 2, True)]),
            ("3. repeat (strike_rep: every 15 min while edge >= 0.05, <= 10 buys/market)",
             [("cap1c", "refill", 10, True), ("cap1c", "no refill", 10, False)]),
            ("4. repeat with no buy limit (every 15 min while edge >= 0.05)",
             [("cap1c", "refill", 10 ** 6, True), ("cap1c", "no refill", 10 ** 6, False)])]
    for title, variants in runs:
        print(f"\n== {title}")
        print(hdr)
        for mode, lab, mb, rf in variants:
          for size in SIZES:
            F, want = run(points, size, mode, mb, rf)
            if not F:
                continue
            cost = sum(f[5] for f in F)
            pnl = sum(f[3] * f[6] - f[5] for f in F)
            byd, bym = defaultdict(float), defaultdict(float)
            for f in F:
                byd[f[1]] += f[3] * f[6] - f[5]
                bym[f[0]] += f[3] * f[6] - f[5]
            print(f"   {lab:16} {size:6d} {len(F):5d} {cost:9.0f} {100 * cost / want:5.0f}% "
                  f"{np.average([f[4] for f in F], weights=[f[3] for f in F]):7.3f} {100 * pnl / cost:+6.1f}% "
                  f"{pnl / nd:+9.2f} {cost / nd:8.0f} {sum(v > 0 for v in byd.values())}/{len(byd)} "
                  f"{min(byd.values()):+10.2f} {pnl - sum(sorted(bym.values(), reverse=True)[:5]):+10.2f}")


if __name__ == "__main__":
    main()
