"""Adaptive repeat buying: buy $1 of a side every time its edge is >= MIN_EDGE,
at most once per SPACING per market, on whichever side qualifies at that moment.

    .venv/bin/python -m research.repeat [--early-only]

Unlike the ladder (adds only when the edge WIDENS on the first side), this adds
whenever the edge PERSISTS, follows the model as it moves with the market, and
can end up holding both sides of one market.  Same data and caveats as
research/zones.py.
"""
from __future__ import annotations

import argparse
from collections import defaultdict

import numpy as np

from polyfin.db import connect
from polyfin.live.fees import fee_per_share
from polyfin.stage1 import load_specs
from polyfin.stage2 import Stage2, collect, evaluate

from .ladder import ret
from .zones import book_index, candidates

MIN_EDGE = 0.05


def edge(r):
    return r[4] - r[5] - fee_per_share(r[5])


def run(rows, spacing_h, max_legs, early_only, min_edge=MIN_EDGE):
    """legs: (cid, side, leg_no, day, ask, win, tau)"""
    by = defaultdict(lambda: defaultdict(list))         # cid -> tau -> [rows (sides)]
    for r in rows:
        if 0.05 <= r[5] <= 0.97 and (not early_only or r[3] > 3):
            by[r[0]][r[3]].append(r)
    legs = []
    for cid, pts in by.items():
        last, n = None, 0
        for tau in sorted(pts, reverse=True):           # chronological
            if n >= max_legs:
                break
            if last is not None and last - tau < spacing_h - 1e-9:
                continue
            best = max(pts[tau], key=edge)
            if edge(best) >= min_edge:
                n += 1
                legs.append((cid, best[1], n, best[2], best[5], best[6], tau))
                last = tau
    return legs


def summarize(name, legs, extra=True):
    if not legs:
        print(f"   {name:34}    -")
        return
    r = np.array([ret(x) for x in legs])
    byday, bymkt, sides = defaultdict(float), defaultdict(float), defaultdict(set)
    for x, v in zip(legs, r):
        byday[x[3]] += v
        bymkt[x[0]] += v
        sides[x[0]].add(x[1])
    per_mkt = np.array([sum(1 for x in legs if x[0] == c) for c in bymkt])
    line = (f"   {name:34} {len(legs):5d} {len(bymkt):4d} {per_mkt.mean():5.1f} {per_mkt.max():4d} "
            f"{100 * np.mean([x[5] > 0.5 for x in legs]):5.0f} {100 * r.mean():+6.1f}% "
            f"{r.sum():+7.2f} {sum(v > 0 for v in byday.values()):2d}/{len(byday):<2d} "
            f"{min(byday.values()):+7.2f}")
    if extra:
        both = sum(1 for s in sides.values() if len(s) > 1)
        line += (f" {100 * both / len(sides):5.0f}% {100 * np.mean([v > 0 for v in bymkt.values()]):5.0f}%"
                 f" {min(bymkt.values()):+6.2f}")
    print(line)


HDR = (f"   {'':34} {'legs':>5} {'mkts':>4} {'/mkt':>5} {'max':>4} {'win%':>5} {'ret/$':>7} "
       f"{'total$':>7} {'days+':>5} {'worst d':>7} {'both':>6} {'mkt+':>6} {'worst m':>7}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--early-only", action="store_true")
    args = ap.parse_args()
    conn = connect()
    specs = load_specs(conn)
    a = collect(Stage2(conn), specs, conn, step=15, hours=36)
    out, _ = evaluate(a)
    rows = candidates(a, out["sharp<=3h"], specs, book_index(conn))
    print(f"{len(set(a['cid']))} resolved liquid markets, {len(set(a['day']))} days"
          f", book prices only"
          f"{', early slot only' if args.early_only else ''}; $1 per buy, edge >= {MIN_EDGE}")
    print("   both = markets where both sides were bought; mkt+ = markets with net profit")

    print("\n== repeat buying: spacing x leg cap")
    print(HDR)
    summarize("single entry (live rule)", run(rows, 99, 1, args.early_only))
    for sp in (0.25, 1, 3):
        for cap in (3, 10, 999):
            summarize(f"every {sp:g}h, max {cap if cap < 999 else 'inf'} buys",
                      run(rows, sp, cap, args.early_only))

    legs = run(rows, 0.25, 999, args.early_only)
    print("\n== by buy number (every 15m, no cap): are late repeats still good?")
    print(HDR)
    for lo, hi, lab in ((1, 1, "buy 1"), (2, 2, "buy 2"), (3, 5, "buys 3-5"), (6, 15, "buys 6-15"),
                        (16, 999, "buys 16+")):
        summarize(lab, [x for x in legs if lo <= x[2] <= hi], extra=False)

    print("\n== buys that flipped to the other side vs the first buy's side (every 15m)")
    print(HDR)
    first = {}
    for x in sorted(legs, key=lambda x: (x[0], x[2])):
        first.setdefault(x[0], x[1])
    summarize("same side as buy 1", [x for x in legs if x[1] == first[x[0]]], extra=False)
    summarize("opposite side (flipped)", [x for x in legs if x[1] != first[x[0]]], extra=False)


if __name__ == "__main__":
    main()
