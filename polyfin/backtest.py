"""Stage 1 backtest on resolved markets.

    python3 -m polyfin.backtest [--step 15] [--hours 36]

For every resolved market, scores the model every `step` minutes over the last
`hours` before its target instant, using a variance profile that excludes the
settlement day, against the Polymarket price at the same instant.  Only
markets that traded at least --min-volume USD are scored: many finance dailies
(FX, NYA, HSI, Nikkei) trade ~$10 a day with a 0.01/0.99 book, and their
"price" is an empty book's midpoint, not a forecast.

Reports:
  proxy     how often our Yahoo settlement proxy agrees with the real outcome
  scores    Brier / log loss, model vs market, on the same (market, t) points
  calib     model calibration by probability bin
"""
from __future__ import annotations

import argparse
import math
from collections import defaultdict

from .data import load_history
from .db import connect
from .stage1 import Model, load_specs

TAU_BUCKETS = [(0, 1), (1, 3), (3, 7), (7, 24), (24, 1e9)]


def brier(p, y):
    return (p - y) ** 2


def logloss(p, y):
    p = min(max(p, 1e-3), 1 - 1e-3)
    return -(y * math.log(p) + (1 - y) * math.log(1 - p))


def proxy_report(model, specs):
    agree = defaultdict(lambda: [0, 0])
    misses = []
    for s in specs:
        if s.outcome is None:
            continue
        ref, st = model.reference(s), model.settle_price(s)
        if ref is None or st is None:
            continue
        y = 1.0 if st > ref else 0.0 if st < ref else 0.5
        a = agree[s.series_slug]
        a[0] += y == s.outcome
        a[1] += 1
        if y != s.outcome:
            misses.append((s, ref, st))
    print("== proxy: Yahoo-derived outcome vs actual resolution")
    tot = [sum(a[0] for a in agree.values()), sum(a[1] for a in agree.values())]
    print(f"   overall {tot[0]}/{tot[1]} = {tot[0] / max(tot[1], 1):.1%}")
    for slug, (k, n) in sorted(agree.items()):
        if k < n:
            print(f"   {slug:34} {k}/{n}")
    for s, ref, st in misses[:15]:
        print(f"   miss {s.series_slug} {s.strike or ''} target={s.target_ts} "
              f"ref={ref:.6g} settle={st:.6g} ({(st / ref - 1) * 1e4:+.1f}bp) outcome={s.outcome}")


def score(model, specs, conn, step, hours):
    pts = []    # (series, kind, tau_h, p_model, p_mkt, y, condition_id)
    for s in specs:
        if s.outcome is None:
            continue
        hist = load_history(conn, s.token_yes)
        ex = model.exclude_for(s)
        t = s.target_ts - hours * 3600
        while t < s.target_ts:
            p = model.prob(s, t, ex)
            m = hist.at(t)
            if p is not None and m is not None:
                pts.append((s.series_slug, s.kind, (s.target_ts - t) / 3600, p, m, s.outcome,
                            s.condition_id))
            t += step * 60
    return pts


def table(title, groups):
    print(f"== {title}")
    print(f"   {'':22} {'n':>6} {'mkts':>5} {'brier model':>11} {'brier mkt':>9} "
          f"{'ll model':>8} {'ll mkt':>7}")
    for name, g in groups:
        if not g:
            continue
        n = len(g)
        mkts = len({x[6] for x in g})
        bm = sum(brier(x[3], x[5]) for x in g) / n
        bk = sum(brier(x[4], x[5]) for x in g) / n
        lm = sum(logloss(x[3], x[5]) for x in g) / n
        lk = sum(logloss(x[4], x[5]) for x in g) / n
        print(f"   {name:22} {n:6d} {mkts:5d} {bm:11.4f} {bk:9.4f} {lm:8.3f} {lk:7.3f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default=None, help="libpq DSN; default from FIN_PG* in .env")
    ap.add_argument("--step", type=int, default=15, help="minutes between scored points")
    ap.add_argument("--hours", type=float, default=36, help="score this far before target")
    ap.add_argument("--min-volume", type=float, default=500, help="USD traded, to be scored")
    args = ap.parse_args()
    conn = connect(args.dsn)
    model = Model(conn)
    specs = load_specs(conn)

    proxy_report(model, specs)
    volume = dict(conn.execute(
        "SELECT condition_id, COALESCE((raw->>'volume')::float, 0) FROM markets"))
    liquid = [s for s in specs if float(volume.get(s.condition_id) or 0) >= args.min_volume]
    skipped = sorted({s.series_slug for s in specs if s.outcome is not None} -
                     {s.series_slug for s in liquid if s.outcome is not None})
    print(f"\nscoring markets with volume >= ${args.min_volume:g}; "
          f"series with none: {', '.join(skipped) or '-'}")
    pts = score(model, liquid, conn, args.step, args.hours)
    print(f"\n{len(pts)} scored points on {len({x[6] for x in pts})} resolved markets\n")

    table("by kind", [("all", pts)] + [(k, [x for x in pts if x[1] == k])
                                         for k in ("updown", "open", "strikes")])
    table("by hours to target", [(f"{a}-{b if b < 1e9 else 'inf'}h",
                                  [x for x in pts if a <= x[2] < b]) for a, b in TAU_BUCKETS])
    by_series = defaultdict(list)
    for x in pts:
        by_series[x[0]].append(x)
    table("by series", sorted(by_series.items()))

    print("== calib (model)")
    bins = defaultdict(list)
    for x in pts:
        bins[min(int(x[3] * 10), 9)].append(x)
    for b in range(10):
        g = bins[b]
        if g:
            print(f"   {b / 10:.1f}-{(b + 1) / 10:.1f}  n={len(g):6d}  "
                  f"mean p={sum(x[3] for x in g) / len(g):.3f}  "
                  f"freq={sum(x[5] for x in g) / len(g):.3f}  "
                  f"mkt p={sum(x[4] for x in g) / len(g):.3f}")


if __name__ == "__main__":
    main()
