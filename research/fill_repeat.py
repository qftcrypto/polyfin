"""Fillability of repeat entry under strict execution: buy at the ask, at most ask + 1c.

    .venv/bin/python -m research.fill_repeat

Real recorded books only (pm_books: Yes ladder; the No ladder is 1 - Yes bids).
The repeat rule of the paper arms (early slot, edge >= 0.05, >= 15 min apart,
<= 10 buys and <= $30 cost per market; hold / flip at 0.05 / flip at 0.10), with
the order limit = min(model limit, best ask + slip).  Each buy walks the actual
ladder up to that limit; partial fills count.

Assumption, unavoidable without live repeats: every later buy sees the book as
recorded, i.e. as if others refilled what we took.  `refill` reports how often
the best-ask level 15 min after a buy could again cover a whole buy.
"""
from __future__ import annotations

import json
import math
from bisect import bisect_right
from collections import defaultdict

import numpy as np

from polyfin.db import connect
from polyfin.live.fees import fee_per_share
from polyfin.live.sizing import limit_price
from polyfin.stage1 import load_specs
from polyfin.stage2 import Stage2, collect, evaluate

SPACING_H, MAX_BUYS, CAP = 0.25, 10, 30.0
ARMS = {"rep_hold": None, "rep_flip": 0.05, "rep_flip10": 0.10}


def ladders(conn):
    """token_yes -> (ts list, [(yes_asks, no_asks)]) with ladders best first."""
    idx = defaultdict(lambda: ([], []))
    for tok, ts, bids, asks in conn.execute(
            "SELECT token_id, ts, bids, asks FROM pm_books ORDER BY token_id, ts"):
        b = bids if isinstance(bids, list) else json.loads(bids or "[]")
        a = asks if isinstance(asks, list) else json.loads(asks or "[]")
        t, v = idx[tok]
        t.append(ts)
        v.append(([(p, z) for p, z in a], [(round(1 - p, 6), z) for p, z in b]))
    conn.commit()
    return idx


def walk(ladder, limit, shares):
    left, cost = shares, 0.0
    for p, z in ladder:
        if p > limit + 1e-9 or left <= 0:
            break
        q = min(left, z)
        cost += q * p
        left -= q
    got = shares - left
    return got, (cost / got if got else None)


def simulate(points, flip, slip, budget):
    """points: per market, chronological [(tau, day, p_yes, yes_asks, no_asks, y)]."""
    buys = []                     # (cid, day, side, want, got, avg, win, slot_refill)
    for cid, pts in points.items():
        last_t = last_side = None
        n, cost = 0, 0.0
        for k, (tau, day, p, ya, na, y) in enumerate(pts):
            if n >= MAX_BUYS or tau <= 3:
                break
            if last_t is not None and last_t - tau < SPACING_H - 1e-9:
                continue
            best = None
            for side, ps, lad, win in (("yes", p, ya, y), ("no", 1 - p, na, 1 - y)):
                if not lad or not 0.05 <= lad[0][0] <= 0.97:
                    continue
                need = 0.05 if last_side in (None, side) else flip
                if need is None:
                    continue
                e = ps - lad[0][0] - fee_per_share(lad[0][0])
                if e >= need and (best is None or e > best[0]):
                    best = (e, side, ps, lad, win, need)
            if best is None:
                continue
            e, side, ps, lad, win, need = best
            mlim = limit_price(ps, need)
            if mlim is None:
                continue
            lim = min(mlim, math.floor((lad[0][0] + slip) * 100 + 1e-9) / 100)
            want = max(5, math.floor(budget / lim + 1e-9))
            if cost + want * lim > CAP + 1e-9:
                continue
            got, avg = walk(lad, lim, want)
            # refill proxy: the same side's best level ~15 min later covers a whole buy?
            later = next((q for q in pts[k + 1:] if q[0] <= tau - SPACING_H), None)
            refill = None
            if later:
                l2 = later[3] if side == "yes" else later[4]
                refill = bool(l2) and l2[0][1] >= want
            buys.append((cid, day, side, want, got, avg, win, refill, lim))
            if got > 0:
                n += 1
                cost += got * avg + got * fee_per_share(avg)
                last_t, last_side = tau, side
    return buys


def report(name, buys):
    if not buys:
        print(f"   {name:30}  -")
        return
    want = np.array([b[3] for b in buys])
    got = np.array([b[4] for b in buys])
    full = np.mean(got >= want - 1e-9)
    part = np.mean((got > 0) & (got < want - 1e-9))
    none = np.mean(got <= 0)
    f = [b for b in buys if b[4] > 0]
    spent = sum(b[4] * (b[5] + fee_per_share(b[5])) for b in f)
    pnl = sum(b[4] * (b[6] - b[5] - fee_per_share(b[5])) for b in f)
    asked = sum(b[3] * b[8] for b in buys)
    byday, bymkt = defaultdict(float), defaultdict(float)
    for b in f:
        v = b[4] * (b[6] - b[5] - fee_per_share(b[5]))
        byday[b[1]] += v
        bymkt[b[0]] += v
    rf = [b[7] for b in f if b[7] is not None]
    print(f"   {name:30} {len(buys):5d} {len(bymkt):4d} {100 * full:5.0f}% {100 * part:5.0f}% "
          f"{100 * none:5.0f}% {100 * spent / asked if asked else 0:6.0f}% "
          f"{100 * pnl / spent if spent else float('nan'):+6.1f}% {pnl:+7.2f} "
          f"{sum(v > 0 for v in byday.values())}/{len(byday)} "
          f"{min(bymkt.values(), default=float('nan')):+7.2f} "
          f"{(100 * np.mean(rf)) if rf else float('nan'):6.0f}%")


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
        ya, na = v[j]
        points[cid].append((a["tau"][i], a["day"][i], p[i], ya, na, a["y"][i]))
    for cid in points:
        points[cid].sort(key=lambda x: -x[0])
    days = sorted({d for pts in points.values() for _, d, *_ in pts})
    print(f"real books only: {len(points)} resolved markets with books, settlement days {days}")
    print(f"repeat rule: early slot, edge >= 0.05, >= 15 min apart, <= {MAX_BUYS} buys, "
          f"<= ${CAP:.0f}/market\n")
    hdr = (f"   {'':30} {'buys':>5} {'mkts':>4} {'full':>6} {'part':>6} {'none':>6} "
           f"{'$fill':>7} {'ret/$':>7} {'pnl$':>7} {'days+':>5} {'worst m':>7} {'refill':>7}")
    for budget in (3.0, 5.0, 10.0):
        for slip in (0.0, 0.01):
            print(f"== ${budget:.0f} per buy, limit = min(model limit, ask + {slip * 100:.0f}c)")
            print(hdr)
            for arm, flip in ARMS.items():
                report(arm, simulate(points, flip, slip, budget))
            print()
    print("full/part/none = share of buy attempts; $fill = $ filled / $ attempted;")
    print("refill = filled buys where the best-ask level 15 min later again covered a whole buy")


if __name__ == "__main__":
    main()
