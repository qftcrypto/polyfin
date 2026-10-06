"""Cheap-side ("dust") buying: low asks where the model sees more.

    .venv/bin/python -m research.dust

For each ask band and edge threshold: one buy per (market, side), the first point
the condition holds, $1 each, return after fee at resolution.  Both price sources
(history mid +/- half spread with untraded 0.500 defaults dropped, 10 days; and
recorded book asks only), plus the model's own calibration in the low-probability
tail - if the model overstates long shots, "cheap" is an illusion.
"""
from __future__ import annotations

from collections import defaultdict

import numpy as np

from polyfin.db import connect
from polyfin.live.fees import fee_per_share
from polyfin.stage1 import load_specs
from polyfin.stage2 import Stage2, collect, evaluate

from .zones import book_index, candidates, half_spreads

BANDS = [(0.01, 0.05), (0.05, 0.10), (0.10, 0.15), (0.15, 0.20), (0.20, 0.30), (0.30, 0.50)]


def edge(r):
    return r[4] - r[5] - fee_per_share(r[5])


def table(rows, title):
    print(f"\n== {title}")
    print(f"   {'ask band':11} {'edge':>5} {'buys':>5} {'mkts':>5} {'avg ask':>7} {'model p':>7} "
          f"{'won':>5} {'ret/$':>8} {'days+':>6}")
    for lo, hi in BANDS:
        for th in (0.05, 0.10, 0.20):
            taken = {}
            for r in sorted(rows, key=lambda r: -r[3]):
                k = (r[0], r[1])
                if k not in taken and lo <= r[5] < hi and edge(r) >= th:
                    taken[k] = r
            t = list(taken.values())
            if not t:
                continue
            rets = np.array([r[6] / (r[5] + fee_per_share(r[5])) - 1 for r in t])
            byd = defaultdict(float)
            for r, x in zip(t, rets):
                byd[r[2]] += x
            print(f"   {lo:.2f}-{hi:.2f}  {th:5.2f} {len(t):5d} {len({r[0] for r in t}):5d} "
                  f"{np.mean([r[5] for r in t]):7.3f} {np.mean([r[4] for r in t]):7.3f} "
                  f"{int(sum(r[6] > 0.5 for r in t)):5d} {100 * rets.mean():+7.1f}% "
                  f"{sum(v > 0 for v in byd.values())}/{len(byd)}")


def main() -> None:
    conn = connect()
    specs = load_specs(conn)
    a = collect(Stage2(conn), specs, conn, step=5, hours=36)
    out, _ = evaluate(a)
    p = out["sharp<=3h"]
    bi, hs = book_index(conn), half_spreads(conn)
    hist = candidates(a, p, specs, bi, hs, False, drop_default=True)
    books = candidates(a, p, specs, bi, hs, True)
    print(f"{len(set(a['cid']))} resolved liquid markets, {len(set(a['day']))} days")
    table(hist, "history-based prices (untraded 0.500 defaults dropped)")
    table(books, "recorded book asks only")

    m, y = a["pm"], a["y"]
    ok = (m > 0.01) & (m < 0.99) & (m != 0.5)
    print("\n== calibration in the tail (both sides folded: P(side) vs how often that side won)")
    ps = np.concatenate([p[ok], 1 - p[ok]])
    ms = np.concatenate([m[ok], 1 - m[ok]])
    ys = np.concatenate([y[ok], 1 - y[ok]])
    print(f"   {'band':11} {'n model':>8} {'model says':>10} {'won':>6} | {'n market':>8} {'market says':>11} {'won':>6}")
    for lo, hi in [(0.0, 0.05), (0.05, 0.10), (0.10, 0.15), (0.15, 0.20), (0.20, 0.30)]:
        g, h = (ps >= lo) & (ps < hi), (ms >= lo) & (ms < hi)
        print(f"   {lo:.2f}-{hi:.2f}  {g.sum():8d} {ps[g].mean():10.3f} {ys[g].mean():6.3f} | "
              f"{h.sum():8d} {ms[h].mean():11.3f} {ys[h].mean():6.3f}")


if __name__ == "__main__":
    main()
