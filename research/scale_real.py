"""Sizing rules under realistic execution, on recorded books only.

    .venv/bin/python -m research.scale_real

Every 5 minutes of each resolved market's early slot, with the leave-one-day-out
stage 2 price and the recorded book (Yes ladder; No ladder = 1 - Yes bids):
  * the target for the market's side is set by the rule (below); a buy is placed
    for (target - filled so far)
  * limit = min(highest price keeping edge >= 0.05 after fee, best ask + 1c),
    whole cents; the order walks the ladder up to the limit, partial fills count
  * whole shares, at least the venue's 5; a shortfall below one minimum order waits
Rules (all one side, never the other; side = first point with edge >= 0.05):
  flat5     $5 at first sight
  ladder2   $5 at first sight, +$5 once the side's edge reaches 0.10 (the live rule)
  cap10     $100 x max edge seen, capped at $10
  prop      $100 x max edge seen, uncapped (first sight 0.07 -> $7, 0.45 -> $45)
"""
from __future__ import annotations

import math
from bisect import bisect_right
from collections import defaultdict

import numpy as np

from polyfin.db import connect
from polyfin.live.fees import fee_per_share
from polyfin.live.sizing import exec_limit
from polyfin.stage1 import load_specs
from polyfin.stage2 import Stage2, collect, evaluate

from .fill_repeat import ladders, walk

START, MIN_SHARES = 0.05, 5


def target(rule, max_edge, first_edge):
    if rule == "flat5":
        return 5.0
    if rule == "ladder2":
        return 10.0 if max_edge >= 0.10 else 5.0
    if rule == "cap10":
        return 100 * min(max_edge, 0.10)
    return 100 * max_edge                                     # prop


FILTERS = {
    "both sides": lambda side, ask: True,
    "Yes/Up only": lambda side, ask: side == "yes",
    "No/Down only": lambda side, ask: side == "no",
    "favourite only (ask>=0.50)": lambda side, ask: ask >= 0.50,
    "underdog only (ask<0.50)": lambda side, ask: ask < 0.50,
}


def simulate(points, rule, allow=FILTERS["both sides"], window="early"):
    """points[cid] = chronological [(tau, day, p_yes, yes_asks, no_asks, y)]"""
    fills, wanted = [], 0.0          # fills: (cid, day, dollars, avg_px, shares, win)
    for cid, pts in points.items():
        side, max_e, first_e, spent = None, 0.0, None, 0.0
        for tau, day, p, ya, na, y in pts:
            if window == "early" and tau <= 3:
                break
            if window == "late" and tau > 3:
                continue
            sides = {"yes": (p, ya, y), "no": (1 - p, na, 1 - y)}
            if side is None:
                best = max(((s, ps - lad[0][0] - fee_per_share(lad[0][0]))
                            for s, (ps, lad, _) in sides.items()
                            if lad and 0.05 <= lad[0][0] <= 0.97 and allow(s, lad[0][0])),
                           key=lambda x: x[1], default=None)
                if best is None or best[1] < START:
                    continue
                side, first_e = best
            ps, lad, win = sides[side]
            if not lad or not 0.05 <= lad[0][0] <= 0.97:
                continue
            e = ps - lad[0][0] - fee_per_share(lad[0][0])
            if e < START:
                continue
            max_e = max(max_e, e)
            short = target(rule, max_e, first_e) - spent
            lim = exec_limit(ps, START, lad[0][0])
            if lim is None or short < MIN_SHARES * lim - 1e-9:
                continue
            n = math.floor(short / lim + 1e-9)
            if n < MIN_SHARES:
                continue
            wanted += n * lim
            got, avg = walk(lad, lim, n)
            if got > 0:
                cost = got * (avg + fee_per_share(avg))
                spent += cost
                fills.append((cid, day, cost, avg, got, win))
    return fills, wanted


def report(name, fills, wanted):
    if not fills:
        print(f"   {name:10} -")
        return
    cost = sum(f[2] for f in fills)
    pnl = sum(f[4] * f[5] - f[2] for f in fills)
    byday, bymkt = defaultdict(float), defaultdict(float)
    per = defaultdict(float)
    for f in fills:
        byday[f[1]] += f[4] * f[5] - f[2]
        bymkt[f[0]] += f[4] * f[5] - f[2]
        per[f[0]] += f[2]
    print(f"   {name:10} {len(fills):5d} {len(per):4d} ${cost:7.0f} {100 * cost / wanted:5.0f}% "
          f"${np.mean(list(per.values())):5.1f} ${max(per.values()):5.0f} {pnl:+8.2f} "
          f"{100 * pnl / cost:+6.1f}%  {sum(v > 0 for v in byday.values())}/{len(byday)} "
          f"{min(byday.values()):+8.2f} {min(bymkt.values()):+7.2f}   "
          + " ".join(f"{d[5:]}:{v:+.0f}" for d, v in sorted(byday.items())))


def main() -> None:
    conn = connect()
    specs = load_specs(conn)
    a = collect(Stage2(conn), specs, conn, step=5, hours=36)
    out, _ = evaluate(a)
    p = out["sharp<=3h"]
    lad = ladders(conn)
    tok = {s.condition_id: s.token_yes for s in specs}
    end = {s.condition_id: s.target_ts for s in specs}
    points = defaultdict(list)
    for i in range(len(a["y"])):
        cid = a["cid"][i]
        t = end[cid] - a["tau"][i] * 3600
        ts, v = lad.get(tok[cid], ([], []))
        j = bisect_right(ts, t) - 1
        if j < 0 or t - ts[j] > 120:
            continue
        points[cid].append((a["tau"][i], a["day"][i], p[i], v[j][0], v[j][1], a["y"][i]))
    for cid in points:
        points[cid].sort(key=lambda x: -x[0])
    days = sorted({d for pts in points.values() for _, d, *_ in pts})
    print(f"recorded books only: {len(points)} resolved markets, settlement days "
          f"{', '.join(str(d) for d in days)}; early slot; buys at <= ask + 1c, >= 5 shares\n")
    print(f"   {'rule':10} {'fills':>5} {'mkts':>4} {'cost':>8} {'fill%':>6} {'$/mkt':>6} "
          f"{'max$':>6} {'pnl':>8} {'ret/$':>7}  days+ {'worst d':>8} {'worst m':>7}   by day")
    import sys
    if "--straddle" in sys.argv:
        for rule in ("flat5", "ladder2"):
            one, _ = simulate(points, rule)
            yes_pts = {c: pts for c, pts in points.items()}
            fy, wy = simulate(yes_pts, rule, FILTERS["Yes/Up only"])
            fn, wn = simulate(yes_pts, rule, FILTERS["No/Down only"])
            both = [(f[0], f[1], f[2], f[3], f[4], f[5]) for f in fy + fn]
            print(f"   -- {rule}")
            report("one side", *simulate(points, rule))
            report("straddle", both, wy + wn)
            sides = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0]))   # cid -> side -> [cost, shares]
            for f in fy:
                sides[f[0]]["yes"][0] += f[2]; sides[f[0]]["yes"][1] += f[4]
            for f in fn:
                sides[f[0]]["no"][0] += f[2]; sides[f[0]]["no"][1] += f[4]
            two = {c: v for c, v in sides.items() if len(v) == 2}
            pair = [sum(v[s][0] / v[s][1] for s in ("yes", "no")) for v in two.values()]
            only_two = [f for f in both if f[0] in two]
            pnl_two = sum(f[4] * f[5] - f[2] for f in only_two)
            print(f"      markets holding both sides: {len(two)} of {len(sides)}; median Up+Down cost "
                  f"per share {np.median(pair) if pair else float('nan'):.3f} "
                  f"({sum(x < 1 for x in pair)} locked a profit, {sum(x >= 1 for x in pair)} a loss); "
                  f"P&L in those markets {pnl_two:+.2f}")
        return
    if "--late" in sys.argv:
        for rule in ("flat5", "ladder2"):
            print(f"   -- {rule}")
            fe, we = simulate(points, rule, window="early")
            fl, wl = simulate(points, rule, window="late")
            report("early only", fe, we)
            report("late only", fl, wl)
            report("early+late", fe + fl, we + wl)
            for lo, hi in ((0, 1), (1, 2), (2, 3)):
                sub = {c: [q for q in pts if lo < q[0] <= hi or (lo == 0 and q[0] <= hi)]
                       for c, pts in points.items()}
                report(f"late {lo}-{hi}h", *simulate(sub, rule, window="late"))
        return
    if "--straddle-dog" in sys.argv:
        dog = FILTERS["underdog only (ask<0.50)"]
        for rule in ("flat5", "ladder2"):
            print(f"   -- {rule}")
            one, w1 = simulate(points, rule)
            report("one side", one, w1)
            # straddle: each side on its own; then split the side bought SECOND in a market
            fy, wy = simulate(points, rule, FILTERS["Yes/Up only"])
            fn, wn = simulate(points, rule, FILTERS["No/Down only"])
            report("straddle", fy + fn, wy + wn)
            first_side = {}
            for f in sorted(one, key=lambda f: f[0]):
                first_side.setdefault(f[0], None)
            # which side did the one-side run pick per market? the side of its fills
            picked = {}
            yes_c = {f[0] for f in fy}
            for f in one:
                picked[f[0]] = "yes" if (f[0] in yes_c and any(abs(f[3] - g[3]) < 1e-9 and f[0] == g[0]
                                                              for g in fy)) else "no"
            extra = [("yes", f) for f in fy if picked.get(f[0]) == "no"] + \
                    [("no", f) for f in fn if picked.get(f[0]) == "yes"]
            for lab, cond in (("extra side, underdog", lambda px: px < 0.50),
                              ("extra side, favourite", lambda px: px >= 0.50)):
                xs = [f for _, f in extra if cond(f[3])]
                pnl = sum(f[4] * f[5] - f[2] for f in xs)
                cost = sum(f[2] for f in xs)
                print(f"      {lab:24} {len(xs):3d} buys, cost ${cost:6.2f}, pnl {pnl:+7.2f}"
                      + (f" ({100 * pnl / cost:+.1f}%)" if cost else ""))
            dy, wdy = simulate(points, rule, lambda side, ask: side == "yes" and ask < 0.50)
            dn, wdn = simulate(points, rule, lambda side, ask: side == "no" and ask < 0.50)
            report("dog 1side", *simulate(points, rule, dog))
            report("dog strad", dy + dn, wdy + wdn)
        return
    if "--sides" in sys.argv:
        for rule in ("flat5", "ladder2"):
            print(f"   -- {rule}")
            for name, f in FILTERS.items():
                report(name[:10], *simulate(points, rule, f))
                print(f"      ({name})")
        return
    for rule in ("flat5", "ladder2", "cap10", "prop"):
        report(rule, *simulate(points, rule))
    print("\n   fill% = dollars filled / dollars ordered (book depth within ask + 1c)")


if __name__ == "__main__":
    main()
