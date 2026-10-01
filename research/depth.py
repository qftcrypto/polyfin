"""Book depth at the moments our arms wanted to buy: how big could a fill be?

    .venv/bin/python -m research.depth

Uses the ask ladder each order recorded at decision time (paper and live), i.e.
real books at moments when the model had an edge.  For each decision:
  top    $ at the best ask
  limit  $ at prices <= our limit (every share that still keeps the required edge)
  2c     $ within 2 cents of the best ask
Ladders are stored to 10 levels, so `limit` is a floor when all 10 are inside it.
"""
from __future__ import annotations

import json
from collections import defaultdict

import numpy as np

from polyfin.db import connect


def group(kind, sym, slug):
    if kind == "strikes":
        return "strikes " + slug.split("-")[0]
    if kind == "open":
        return "opens"
    if sym.endswith("=F"):
        return "commodity up/down"
    if sym.startswith("^"):
        return "index up/down"
    return "stock/ETF up/down"


def main() -> None:
    conn = connect()
    rows = conn.execute(
        "SELECT DISTINCT ON (o.condition_id, o.side, o.created_at) o.kind, m.symbol, "
        "o.series_slug, o.limit_price, o.asks FROM trade.orders o JOIN markets m "
        "USING (condition_id) WHERE o.asks IS NOT NULL "
        "ORDER BY o.condition_id, o.side, o.created_at").fetchall()
    g = defaultdict(lambda: defaultdict(list))
    for kind, sym, slug, lim, asks in rows:
        a = asks if isinstance(asks, list) else json.loads(asks)
        if not a:
            continue
        top = a[0][0] * a[0][1]
        inlim = sum(p * z for p, z in a if p <= lim + 1e-9)
        in2c = sum(p * z for p, z in a if p <= a[0][0] + 0.02 + 1e-9)
        for k in (group(kind, sym or "", slug), "ALL"):
            g[k]["top"].append(top)
            g[k]["lim"].append(inlim)
            g[k]["2c"].append(in2c)
    print(f"{len(rows)} distinct decision-time books (paper + live)\n")
    print(f"{'group':20} {'n':>4} | {'$ at best ask p25/med/p75':>27} | "
          f"{'$ within limit p25/med/p75':>28} | {'2c med':>7} | "
          f"{'limit>=$3':>9} {'>=$15':>6} {'>=$30':>6} {'>=$100':>7}")
    for k in sorted(g, key=lambda k: (k == "ALL", k)):
        t, lim, c2 = (np.array(g[k][x]) for x in ("top", "lim", "2c"))
        q = lambda v: "/".join(f"{np.percentile(v, p):.0f}" for p in (25, 50, 75))
        print(f"{k:20} {len(lim):4d} | {q(t):>27} | {q(lim):>28} | {np.median(c2):7.0f} | "
              f"{100 * np.mean(lim >= 3):8.0f}% {100 * np.mean(lim >= 15):5.0f}% "
              f"{100 * np.mean(lim >= 30):5.0f}% {100 * np.mean(lim >= 100):6.0f}%")

    live = conn.execute(
        "SELECT status, COUNT(*), ROUND(SUM(shares_filled)::numeric, 1), "
        "ROUND(SUM(shares_req)::numeric, 1) FROM trade.orders WHERE mode = 'live' "
        "GROUP BY 1").fetchall()
    print("\nlive orders (status, count, shares filled, shares requested):", live)


if __name__ == "__main__":
    main()
