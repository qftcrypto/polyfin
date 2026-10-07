"""Strategy zone scan: where, in (model probability, ask, time-to-target) space,
buying a side of a resolved daily market made money.

    .venv/bin/python -m research.zones [--hours 36] [--step 15]

Method
------
* Model: stage 2, leave-one-day-out (`stage2.evaluate`, variant sharp<=3h), so
  no day is scored by parameters fitted on it.
* Every `step` minutes of each resolved liquid market, both sides are candidate
  buys: Yes at the Yes ask, No at the No ask (= 1 - Yes bid).
* Prices: recorded book snapshots only (within 2 min of the point).  Points with
  no snapshot are skipped.  The old fallback - CLOB price-history mid +/- half a
  spread - was removed on 2026-10-06: those prices are stale (history ask 0.468
  vs the next real ask 0.709; edge +0.196 vs -0.045), so it manufactured edge
  and every result built on it was invalid.
* A zone takes ONE entry per market and side: the first point that qualifies.
  Per-trade P&L = outcome - ask - taker fee, per $1-payout contract.
* Same-day markets move together, so results are also shown per day; a zone
  that wins on 3 of 7 days is not the same as one that wins on 7 of 7.
"""
from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import defaultdict

import numpy as np

from polyfin.db import connect
from polyfin.live.fees import fee_per_share
from polyfin.stage1 import load_specs
from polyfin.stage2 import Stage2, collect, evaluate


def book_index(conn):
    """token -> (ts list, [(best_bid, best_ask)])"""
    idx = defaultdict(lambda: ([], []))
    for tok, ts, bb, ba in conn.execute(
            "SELECT token_id, ts, best_bid, best_ask FROM pm_books ORDER BY token_id, ts"):
        t, v = idx[tok]
        t.append(ts)
        v.append((bb, ba))
    conn.commit()
    return idx


def candidates(a, p, specs, books):
    """One row per (point, side) with a recorded book: cid, side, day, tau, p_side,
    ask, win, source.  Real book prices only - see the module docstring."""
    tok = {s.condition_id: s.token_yes for s in specs}
    series = {s.condition_id: s.series_slug for s in specs}
    end = {s.condition_id: s.target_ts for s in specs}
    rows = []
    for i in range(len(a["y"])):
        cid = a["cid"][i]
        t = end[cid] - a["tau"][i] * 3600
        bt, bv = books.get(tok[cid], ([], []))
        j = bisect_right(bt, t) - 1
        if j < 0 or t - bt[j] > 120:
            continue
        bid, ask = bv[j]
        src = "book"
        y = a["y"][i]
        if ask is not None and 0 < ask < 1:
            rows.append((cid, "yes", a["day"][i], a["tau"][i], p[i], ask, y, src, series[cid]))
        if bid is not None and 0 < bid < 1:
            rows.append((cid, "no", a["day"][i], a["tau"][i], 1 - p[i], 1 - bid, 1 - y, src,
                         series[cid]))
    return rows


def zone(rows, name, cond):
    """First qualifying entry per (market, side), chronological (largest tau first)."""
    taken = {}
    for r in sorted(rows, key=lambda r: -r[3]):
        k = (r[0], r[1])
        if k not in taken and cond(r):
            taken[k] = r
    t = list(taken.values())
    if not t:
        return name, None
    pnl = np.array([r[6] - r[5] - fee_per_share(r[5]) for r in t])
    cost = np.array([r[5] + fee_per_share(r[5]) for r in t])
    byday = defaultdict(float)
    for r, x in zip(t, pnl):
        byday[r[2]] += x
    return name, {
        "n": len(t), "mkts": len({r[0] for r in t}), "days": len(byday),
        "win": float(np.mean([r[6] > 0.5 for r in t])), "ask": float(np.mean([r[5] for r in t])),
        "p": float(np.mean([r[4] for r in t])), "pnl": float(pnl.mean()),
        "roi": float(pnl.sum() / cost.sum()), "total": float(pnl.sum()),
        "pos_days": sum(1 for v in byday.values() if v > 0), "worst_day": min(byday.values()),
        "book": float(np.mean([r[7] == "book" for r in t])),
    }


def show(title, results):
    print(f"\n== {title}")
    print(f"   {'zone':44} {'n':>4} {'mkts':>4} {'win%':>5} {'ask':>5} {'p':>5} "
          f"{'pnl/c':>7} {'ROI':>6} {'total':>7} {'days+':>6} {'worst':>6} {'book%':>5}")
    for name, r in results:
        if r is None:
            print(f"   {name:44}    -")
            continue
        print(f"   {name:44} {r['n']:4d} {r['mkts']:4d} {100 * r['win']:5.0f} {r['ask']:5.2f} "
              f"{r['p']:5.2f} {r['pnl']:+7.3f} {100 * r['roi']:+5.0f}% {r['total']:+7.2f} "
              f"{r['pos_days']:2d}/{r['days']:<3d} {r['worst_day']:+6.2f} {100 * r['book']:5.0f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=36)
    ap.add_argument("--step", type=int, default=15)
    args = ap.parse_args()
    conn = connect()
    specs = load_specs(conn)
    a = collect(Stage2(conn), specs, conn, step=args.step, hours=args.hours)
    out, _ = evaluate(a)
    p = out["sharp<=3h"]
    rows = candidates(a, p, specs, book_index(conn))
    print(f"{len(set(a['cid']))} resolved liquid markets over {len(set(a['day']))} days, "
          f"{len(rows)} candidate (point, side) buys"
          " - recorded book prices only")

    edge = lambda r: r[4] - r[5] - fee_per_share(r[5])
    E, L = (lambda r: r[3] > 3), (lambda r: r[3] <= 3)          # early / late slot

    show("baseline: the live rule (edge >= 0.05, ask 0.05-0.97)", [
        ("all windows", zone(rows, "", lambda r: edge(r) >= 0.05 and 0.05 <= r[5] <= 0.97)[1]),
        ("early slot (> 3h)", zone(rows, "", lambda r: E(r) and edge(r) >= 0.05
                                   and 0.05 <= r[5] <= 0.97)[1]),
        ("late slot (<= 3h)", zone(rows, "", lambda r: L(r) and edge(r) >= 0.05
                                   and 0.05 <= r[5] <= 0.97)[1]),
    ])

    show("edge bands (ask 0.05-0.97, all windows) - deeper dissent", [
        (f"edge {lo:.2f}-{hi:.2f}", zone(rows, "", lambda r, lo=lo, hi=hi:
                                         lo <= edge(r) < hi and 0.05 <= r[5] <= 0.97)[1])
        for lo, hi in [(0.0, 0.05), (0.05, 0.10), (0.10, 0.15), (0.15, 0.20), (0.20, 0.30),
                       (0.30, 1.0)]])

    show("edge thresholds (first entry at or above)", [
        (f"edge >= {th:.2f}", zone(rows, "", lambda r, th=th: edge(r) >= th
                                   and 0.05 <= r[5] <= 0.97)[1])
        for th in (0.05, 0.10, 0.15, 0.20, 0.30)])

    show("dust: ask <= 0.05 where the model gives at least p", [
        (f"ask <= 0.05, p >= {pt:.2f}", zone(rows, "", lambda r, pt=pt: r[5] <= 0.05
                                             and r[4] >= pt)[1])
        for pt in (0.10, 0.15, 0.25, 0.35)] + [
        (f"ask <= 0.10, p >= {pt:.2f}", zone(rows, "", lambda r, pt=pt: r[5] <= 0.10
                                             and r[4] >= pt)[1])
        for pt in (0.20, 0.30)])

    show("by side of the market: underdog (ask < 0.5) vs favourite (ask >= 0.5), edge >= 0.05", [
        ("underdog, early", zone(rows, "", lambda r: E(r) and r[5] < 0.5 and edge(r) >= 0.05
                                 and r[5] >= 0.05)[1]),
        ("underdog, late", zone(rows, "", lambda r: L(r) and r[5] < 0.5 and edge(r) >= 0.05
                                and r[5] >= 0.05)[1]),
        ("favourite (agreement), early", zone(rows, "", lambda r: E(r) and r[5] >= 0.5
                                              and edge(r) >= 0.05 and r[5] <= 0.97)[1]),
        ("favourite (agreement), late", zone(rows, "", lambda r: L(r) and r[5] >= 0.5
                                             and edge(r) >= 0.05 and r[5] <= 0.97)[1]),
        ("favourite >= 0.85, late, edge >= 0.03", zone(rows, "", lambda r: L(r) and r[5] >= 0.85
                                                       and edge(r) >= 0.03 and r[5] <= 0.98)[1]),
    ])

    show("ask bands x edge >= 0.05 (all windows)", [
        (f"ask {lo:.2f}-{hi:.2f}", zone(rows, "", lambda r, lo=lo, hi=hi: lo <= r[5] < hi
                                        and edge(r) >= 0.05)[1])
        for lo, hi in [(0.01, 0.05), (0.05, 0.15), (0.15, 0.30), (0.30, 0.50), (0.50, 0.70),
                       (0.70, 0.85), (0.85, 0.97)]])

    show("time to target x edge >= 0.05 (ask 0.05-0.97)", [
        (f"{lo}-{hi}h", zone(rows, "", lambda r, lo=lo, hi=hi: lo <= r[3] < hi
                             and edge(r) >= 0.05 and 0.05 <= r[5] <= 0.97)[1])
        for lo, hi in [(0, 1), (1, 3), (3, 7), (7, 16), (16, 24), (24, 36)]])

    kind_of = {s.series_slug: s.kind for s in specs}
    show("by market kind, edge >= 0.05 (ask 0.05-0.97)", [
        (k, zone(rows, "", lambda r, k=k: kind_of.get(r[8]) == k and edge(r) >= 0.05
                 and 0.05 <= r[5] <= 0.97)[1])
        for k in sorted(set(kind_of.values()))])


if __name__ == "__main__":
    main()
