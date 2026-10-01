"""Does buying the other side after a flip work as a hedge?

    .venv/bin/python -m research.hedge [--books-only] [--early-only]

Takes the repeat strategy (every 15m, edge >= 0.05, either side) and, for the
markets where it bought BOTH sides, compares the market's net P&L with and
without the flipped (opposite-side) buys.  Share-based: a hedged Up+Down pair
pays exactly 1, so it profits only if the two average prices sum to < 1.
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
from .repeat import run
from .zones import book_index, candidates, half_spreads


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--books-only", action="store_true")
    ap.add_argument("--early-only", action="store_true")
    args = ap.parse_args()
    conn = connect()
    specs = load_specs(conn)
    a = collect(Stage2(conn), specs, conn, step=15, hours=36)
    out, _ = evaluate(a)
    rows = candidates(a, out["sharp<=3h"], specs, book_index(conn), half_spreads(conn),
                      args.books_only)
    for cap in (10, 999):
        legs = run(rows, 0.25, cap, args.early_only)
        by = defaultdict(list)
        for x in legs:
            by[x[0]].append(x)
        flipped = {c: ls for c, ls in by.items() if len({x[1] for x in ls}) > 1}
        print(f"\n== repeat every 15m, max {cap if cap < 999 else 'inf'} buys: "
              f"{len(flipped)} of {len(by)} markets bought both sides")
        with_f, without_f, sums, locked = [], [], [], 0
        for c, ls in flipped.items():
            first = min(ls, key=lambda x: x[2])[1]
            same = [x for x in ls if x[1] == first]
            opp = [x for x in ls if x[1] != first]
            with_f.append(sum(ret(x) for x in ls))
            without_f.append(sum(ret(x) for x in same))
            # average price paid per share on each side (equal-$ buys)
            avg = lambda xs: len(xs) / sum(1 / (x[4] + fee_per_share(x[4])) for x in xs)
            s = avg(same) + avg(opp)
            sums.append(s)
            locked += s > 1
        w, wo = np.array(with_f), np.array(without_f)
        print(f"   net P&L over these markets   without flips {wo.sum():+8.2f}   "
              f"with flips {w.sum():+8.2f}   (flip buys added {w.sum() - wo.sum():+.2f})")
        print(f"   per-market spread (std)      without {wo.std():6.2f}   with {w.std():6.2f}")
        print(f"   worst market                 without {wo.min():+7.2f}   with {w.min():+7.2f}")
        print(f"   avg price first side + avg price other side: median {np.median(sums):.3f}; "
              f"{locked} of {len(sums)} markets > 1.00 (pair locked in a loss)")
        hurt = sum(1 for x, y in zip(w, wo) if x < y)
        print(f"   flips made the market worse in {hurt} of {len(w)} markets")


if __name__ == "__main__":
    main()
