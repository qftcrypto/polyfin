"""Scale-in ("ladder") test: add to a position when the edge on the SAME side widens.

    .venv/bin/python -m research.ladder [--books-only] [--early-only]

Leg k is a fixed $1 buy (scale to $3 at will) at the first point, after leg k-1,
where the edge (model p - ask - fee) on the side leg 1 chose reaches RUNGS[k].
Widening usually means the market moved against us, so the add-on legs are
averaging down; each leg is scored on its own (dollar return after fees) to
separate "the first entry pays" from "adding pays".

Same data and caveats as research/zones.py (leave-one-day-out stage 2, book
prices where recorded, else history mid +/- half spread).
"""
from __future__ import annotations

import argparse
from collections import defaultdict

import numpy as np

from polyfin.db import connect
from polyfin.live.fees import fee_per_share
from polyfin.stage1 import load_specs
from polyfin.stage2 import Stage2, collect, evaluate

from .zones import book_index, candidates, half_spreads

LADDERS = {
    "single 0.05 (live rule)": [0.05],
    "single 0.10": [0.10],
    "ladder 0.05 + 0.10": [0.05, 0.10],
    "ladder 0.05 + 0.10 + 0.15": [0.05, 0.10, 0.15],
    "ladder 0.05 + 0.10 + 0.15 + 0.20": [0.05, 0.10, 0.15, 0.20],
}


def edge(r):
    return r[4] - r[5] - fee_per_share(r[5])


def run_ladder(rows, rungs, early_only):
    """legs: list of (cid, side, leg_no, day, ask, win, tau)."""
    by = defaultdict(list)
    for r in rows:
        if 0.05 <= r[5] <= 0.97 and (not early_only or r[3] > 3):
            by[(r[0], r[1])].append(r)
    # leg 1 picks the side: for each market, the side whose rung-1 signal came first
    first = {}
    for (cid, side), rs in by.items():
        rs.sort(key=lambda r: -r[3])                    # chronological
        hit = next((r for r in rs if edge(r) >= rungs[0]), None)
        if hit and (cid not in first or hit[3] > first[cid][3]):
            first[cid] = hit
    legs = []
    for cid, h in first.items():
        rs = by[(cid, h[1])]
        legs.append((cid, h[1], 1, h[2], h[5], h[6], h[3]))
        tau = h[3]
        for k, rung in enumerate(rungs[1:], start=2):
            nxt = next((r for r in rs if r[3] < tau and edge(r) >= rung), None)
            if nxt is None:
                break
            legs.append((cid, h[1], k, nxt[2], nxt[5], nxt[6], nxt[3]))
            tau = nxt[3]
    return legs


def ret(leg):
    """Dollar return of a $1 buy: shares = 1/(ask+fee), payout = shares * win."""
    ask, win = leg[4], leg[5]
    return win / (ask + fee_per_share(ask)) - 1


def summarize(name, legs):
    if not legs:
        print(f"   {name:36}    -")
        return
    r = np.array([ret(x) for x in legs])
    byday = defaultdict(float)
    for x, v in zip(legs, r):
        byday[x[3]] += v
    print(f"   {name:36} {len(legs):4d} {len({x[0] for x in legs}):4d} "
          f"{100 * np.mean([x[5] > 0.5 for x in legs]):5.0f} {np.mean([x[4] for x in legs]):5.2f} "
          f"{np.mean([x[6] for x in legs]):5.1f} {100 * r.mean():+6.1f}% {r.sum():+7.2f} "
          f"{sum(v > 0 for v in byday.values()):2d}/{len(byday):<2d} {min(byday.values()):+6.2f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--books-only", action="store_true")
    ap.add_argument("--early-only", action="store_true", help="only entries > 3h out (live)")
    ap.add_argument("--hours", type=float, default=36)
    args = ap.parse_args()
    conn = connect()
    specs = load_specs(conn)
    a = collect(Stage2(conn), specs, conn, step=15, hours=args.hours)
    out, _ = evaluate(a)
    rows = candidates(a, out["sharp<=3h"], specs, book_index(conn), half_spreads(conn),
                      args.books_only)
    print(f"{len(set(a['cid']))} resolved liquid markets, {len(set(a['day']))} days"
          f"{', book prices only' if args.books_only else ''}"
          f"{', early slot only' if args.early_only else ''}; $1 per leg")
    hdr = (f"   {'':36} {'legs':>4} {'mkts':>4} {'win%':>5} {'ask':>5} {'tau h':>5} "
           f"{'ret/$':>7} {'total$':>7} {'days+':>5} {'worst':>6}")

    print("\n== whole ladders (all legs together)")
    print(hdr)
    results = {}
    for name, rungs in LADDERS.items():
        legs = run_ladder(rows, rungs, args.early_only)
        results[name] = legs
        summarize(name, legs)

    full = results["ladder 0.05 + 0.10 + 0.15 + 0.20"]
    print("\n== each leg on its own (from the 4-rung ladder)")
    print(hdr)
    for k in (1, 2, 3, 4):
        summarize(f"leg {k} (edge >= {[0.05, 0.10, 0.15, 0.20][k - 1]:.2f})",
                  [x for x in full if x[2] == k])

    print("\n== does widening predict the first leg losing?")
    print(hdr)
    added = {x[0] for x in full if x[2] == 2}
    summarize("leg 1, markets that later widened", [x for x in full if x[2] == 1 and x[0] in added])
    summarize("leg 1, markets that did not", [x for x in full if x[2] == 1 and x[0] not in added])


if __name__ == "__main__":
    main()
