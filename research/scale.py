"""Divergence-proportional sizing: hold $1 per 0.01 of edge, one side, top-ups only.

    .venv/bin/python -m research.scale [--all-windows] [--step 5]

Per market: the first point where a side's edge (model - ask - fee) reaches
START (0.05) fixes the side and buys $100 x edge (0.05 -> $5).  Afterwards, at
every point where that SAME side's edge sets a new high, buy the difference so
the dollars invested equal $100 x edge (0.07 -> +$2, 0.10 -> +$3 ...).  A
narrowing edge does nothing; the other side is never bought.  Compared with a
flat $5 single entry at the first 0.05 on the same markets.

Same data and caveats as research/zones.py (leave-one-day-out stage 2; book
prices where recorded, else history mid +/- the series' half spread).  History
points sitting at exactly 0.500 (an untraded market's default) are dropped.
"""
from __future__ import annotations

import argparse
from collections import defaultdict

import numpy as np

from polyfin.db import connect
from polyfin.live.fees import fee_per_share
from polyfin.stage1 import load_specs
from polyfin.stage2 import Stage2, collect, evaluate

from .zones import book_index, candidates

START = 0.05
PER_POINT = 100.0          # $ per 1.00 of edge = $1 per 0.01


def edge(r):
    return r[4] - r[5] - fee_per_share(r[5])


def run(rows, early_only, cap=None):
    """buys: (cid, day, side, dollars, ask, win, edge, buy_no).  `cap`: stop topping
    up once the target reaches $100 x cap (0.10 -> $10 per market)."""
    by = defaultdict(list)
    for r in rows:
        if 0.05 <= r[5] <= 0.97 and (not early_only or r[3] > 3):
            by[r[0]].append(r)
    scaled, flat = [], []
    for cid, rs in by.items():
        rs.sort(key=lambda r: -r[3])                     # chronological
        side, invested, n = None, 0.0, 0
        for r in rs:
            if side is None:
                if edge(r) >= START and all(edge(q) <= edge(r) for q in rs if q[3] == r[3]):
                    side = r[1]
                    flat.append((cid, r[2], side, 5.0, r[5], r[6], edge(r), 1))
                else:
                    continue
            if r[1] != side:
                continue
            target = PER_POINT * (edge(r) if cap is None else min(edge(r), cap))
            if target > invested + 1e-9 and edge(r) >= START:
                n += 1
                scaled.append((cid, r[2], side, target - invested, r[5], r[6], edge(r), n))
                invested = target
    return scaled, flat


def run_ladder2(rows, early_only, size=5.0):
    """The live rule: $size at the first 0.05, one more $size if that side reaches 0.10."""
    by = defaultdict(list)
    for r in rows:
        if 0.05 <= r[5] <= 0.97 and (not early_only or r[3] > 3):
            by[r[0]].append(r)
    buys = []
    for cid, rs in by.items():
        rs.sort(key=lambda r: -r[3])
        side, leg = None, 0
        for r in rs:
            if side is None:
                if edge(r) >= START and all(edge(q) <= edge(r) for q in rs if q[3] == r[3]):
                    side, leg = r[1], 1
                    buys.append((cid, r[2], side, size, r[5], r[6], edge(r), 1))
                continue
            if leg == 1 and r[1] == side and edge(r) >= 0.10:
                leg = 2
                buys.append((cid, r[2], side, size, r[5], r[6], edge(r), 2))
                break
    return buys


def summarize(name, buys):
    if not buys:
        print(f"   {name:34} -")
        return
    cost = sum(b[3] for b in buys)
    pnl = sum(b[3] * (b[5] / (b[4] + fee_per_share(b[4])) - 1) for b in buys)
    byday, bymkt = defaultdict(float), defaultdict(float)
    for b in buys:
        v = b[3] * (b[5] / (b[4] + fee_per_share(b[4])) - 1)
        byday[b[1]] += v
        bymkt[b[0]] += v
    per_mkt = defaultdict(float)
    for b in buys:
        per_mkt[b[0]] += b[3]
    print(f"   {name:34} {len(buys):5d} {len(bymkt):4d} ${cost:8.0f} ${np.mean(list(per_mkt.values())):5.1f} "
          f"${max(per_mkt.values()):5.0f} {pnl:+9.2f} {100 * pnl / cost:+6.1f}% "
          f"{sum(v > 0 for v in byday.values())}/{len(byday)} {min(byday.values()):+8.2f} "
          f"{min(bymkt.values()):+7.2f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all-windows", action="store_true", help="default: early slot only")
    ap.add_argument("--step", type=int, default=5, help="minutes between points")
    args = ap.parse_args()
    conn = connect()
    specs = load_specs(conn)
    a = collect(Stage2(conn), specs, conn, step=args.step, hours=36)
    out, _ = evaluate(a)
    rows = candidates(a, out["sharp<=3h"], specs, book_index(conn))
    scaled, flat = run(rows, not args.all_windows)
    capped, _ = run(rows, not args.all_windows, cap=0.10)
    ladder2 = run_ladder2(rows, not args.all_windows)
    print(f"{len(set(a['cid']))} resolved liquid markets, {len(set(a['day']))} days, points every "
          f"{args.step} min, book prices only, "
          f"{'all windows' if args.all_windows else 'early slot only'}")
    print(f"   {'':34} {'buys':>5} {'mkts':>4} {'cost':>9} {'$/mkt':>6} {'max$':>6} {'pnl':>9} "
          f"{'ret/$':>7} {'days+':>5} {'worst d':>8} {'worst m':>7}")
    summarize("flat $5 at first 0.05", flat)
    summarize("two buys $5 @0.05 + $5 @0.10 (live)", ladder2)
    summarize("$1 per 0.01, top-ups to D=0.10", capped)
    summarize("   top-ups only (capped)", [b for b in capped if b[7] > 1])
    summarize("$1 per 0.01, top-ups uncapped", scaled)
    summarize("   first buy only ($5)", [b for b in scaled if b[7] == 1])
    summarize("   top-ups only", [b for b in scaled if b[7] > 1])
    print("\n   top-ups by the edge at which they were bought:")
    for lo, hi in [(0.05, 0.10), (0.10, 0.15), (0.15, 0.25), (0.25, 1.0)]:
        summarize(f"   edge {lo:.2f}-{hi:.2f}", [b for b in scaled if b[7] > 1 and lo <= b[6] < hi])


if __name__ == "__main__":
    main()
