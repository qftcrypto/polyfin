"""Is the model still at least as good as the market?  Especially where they disagree.

    .venv/bin/python -m research.model_check

Leave-one-day-out stage 2 (variant sharp<=3h, what live prices with), scored
against the CLOB price-history price at the same instant, on resolved liquid
markets.  The trading premise - buy when model - ask >= 0.05 - needs the model to
be right MORE often than the market exactly where the two disagree, so section 3
scores only those points.
"""
from __future__ import annotations

from collections import defaultdict

import numpy as np

from polyfin.db import connect
from polyfin.stage1 import load_specs
from polyfin.stage2 import Stage2, collect, evaluate


def brier(p, y):
    return float(((p - y) ** 2).mean())


def main() -> None:
    conn = connect()
    a = collect(Stage2(conn), load_specs(conn), conn, step=15, hours=36)
    out, _ = evaluate(a)
    p, m, y, tau, day = out["sharp<=3h"], a["pm"], a["y"], a["tau"], a["day"]
    ok = (m > 0.01) & (m < 0.99) & (m != 0.5)        # drop untraded / default prices
    p, m, y, tau, day = p[ok], m[ok], y[ok], tau[ok], day[ok]
    print(f"{ok.sum()} scored points, {len(set(a['cid'][ok]))} markets, {len(set(day))} days\n")

    print("== 1. Brier (lower is better), held-out days")
    print(f"   {'':16} {'n':>6} {'model':>7} {'market':>7} {'better':>7}")
    groups = [("all", np.ones(len(y), bool)), ("early (>3h)", tau > 3), ("late (<=3h)", tau <= 3)]
    groups += [(f"day {d}", day == d) for d in sorted(set(day))]
    for name, g in groups:
        if g.sum() > 20:
            bm, bk = brier(p[g], y[g]), brier(m[g], y[g])
            print(f"   {name:16} {g.sum():6d} {bm:7.4f} {bk:7.4f} {'model' if bm < bk else 'MARKET':>7}")

    print("\n== 2. calibration, early slot: predicted vs realized")
    e = tau > 3
    print(f"   {'bin':9} {'n':>5} {'model':>6} {'freq':>6} | {'n':>5} {'market':>6} {'freq':>6}")
    for lo, hi in zip([0, .1, .2, .3, .4, .5, .6, .7, .8, .9], [.1, .2, .3, .4, .5, .6, .7, .8, .9, 1.01]):
        a1, b1 = e & (p >= lo) & (p < hi), e & (m >= lo) & (m < hi)
        f = lambda s: f"{y[s].mean():6.3f}" if s.sum() else "     -"
        print(f"   {lo:.1f}-{min(hi, 1):.1f} {a1.sum():5d} {p[a1].mean() if a1.sum() else 0:6.3f} {f(a1)} | "
              f"{b1.sum():5d} {m[b1].mean() if b1.sum() else 0:6.3f} {f(b1)}")

    print("\n== 3. where they DISAGREE (early slot): who is closer to the outcome?")
    print("   gap = |model - market|; 'realized' = how often the side the model favours won")
    print(f"   {'gap':10} {'n':>5} {'mkts':>5} {'model says':>10} {'market says':>11} {'realized':>8} "
          f"{'brier model':>11} {'brier mkt':>9} {'days model better':>17}")
    d = p - m
    for lo, hi in [(0.05, 0.10), (0.10, 0.15), (0.15, 0.25), (0.25, 1.0), (0.05, 1.0)]:
        g = e & (np.abs(d) >= lo) & (np.abs(d) < hi)
        if g.sum() < 5:
            continue
        # orient every point on the side the model favours vs the market
        up = d[g] > 0
        ps = np.where(up, p[g], 1 - p[g])
        ms = np.where(up, m[g], 1 - m[g])
        ys = np.where(up, y[g], 1 - y[g])
        dd = defaultdict(lambda: [0.0, 0.0])
        for dy, pi, mi, yi in zip(day[g], p[g], m[g], y[g]):
            dd[dy][0] += (pi - yi) ** 2
            dd[dy][1] += (mi - yi) ** 2
        better = sum(v[0] < v[1] for v in dd.values())
        print(f"   {lo:.2f}-{hi if hi < 1 else 1:.2f}  {g.sum():5d} {len(set(a['cid'][ok][g])):5d} "
              f"{ps.mean():10.3f} {ms.mean():11.3f} {ys.mean():8.3f} {brier(p[g], y[g]):11.4f} "
              f"{brier(m[g], y[g]):9.4f} {better:8d} / {len(dd)}")


if __name__ == "__main__":
    main()
