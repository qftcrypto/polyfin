"""Are we racing anyone?  Fills, and price drift after our entries.

    .venv/bin/python -m research.race

polycrypto/doc/event_driven.md: on 5m crypto, mispriced asks lived < 1s and 26%
of live orders voided.  polyfin decides every 30s on REST books and Yahoo 1m
bars that are 1-3 minutes old, so two different questions:

1. Execution: do live orders lose the ask while in flight (no-fill / partial)?
2. Staleness / adverse selection: after we buy, does the price of what we bought
   keep FALLING (we arrived after the market had already repriced against our
   stale inputs) or RISE (the market comes our way, or holds)?  Drift is the
   side's CLOB price-history mid at +1m / +5m / +30m / +2h minus its mid at entry.
"""
from __future__ import annotations

from collections import defaultdict

import numpy as np

from polyfin.data import load_history
from polyfin.db import connect

HORIZONS = [(60, "+1m"), (300, "+5m"), (1800, "+30m"), (7200, "+2h")]


def main() -> None:
    conn = connect()
    ex = conn.execute(
        "SELECT status, COUNT(*), SUM(shares_req), SUM(shares_filled), "
        "AVG(filled_at - created_at) FILTER (WHERE filled_at IS NOT NULL) "
        "FROM trade.orders WHERE mode = 'live' GROUP BY 1 ORDER BY 1").fetchall()
    n = sum(r[1] for r in ex)
    print(f"== 1. live execution ({n} orders)")
    for st, c, req, got, lat in ex:
        print(f"   {st:8} {c:4d} ({100 * c / n:4.1f}%)  shares {got or 0:.0f}/{req or 0:.0f}"
              + (f"  decide->fill {lat:.1f}s" if lat is not None else ""))
    slip = conn.execute(
        "SELECT AVG(avg_price - best_ask), MAX(avg_price - best_ask) FROM trade.orders "
        "WHERE mode = 'live' AND shares_filled > 0").fetchone()
    print(f"   paid vs ask seen: mean {slip[0]:+.4f}, worst {slip[1]:+.4f}")

    rows = conn.execute(
        "SELECT o.mode, o.arm, o.side, o.created_at, o.avg_price, m.token_yes, o.tau_h, o.edge "
        "FROM trade.orders o JOIN markets m USING (condition_id) "
        "WHERE o.shares_filled > 0 AND o.slot = 'early' AND COALESCE(o.leg, 1) = 1 "
        "AND ((o.mode = 'live') OR (o.mode = 'paper' AND o.arm = 'base'))").fetchall()
    conn.commit()
    hist = {}
    drift = defaultdict(lambda: defaultdict(list))
    for mode, arm, side, t0, avg, tok, tau, edge in rows:
        if tok not in hist:
            hist[tok] = load_history(conn, tok)
        h = hist[tok]
        p0 = h.at(t0, max_age=300)
        if p0 is None:
            continue
        s0 = p0 if side == "yes" else 1 - p0
        for dt, lab in HORIZONS:
            p = h.at(t0 + dt, max_age=300)
            if p is not None:
                s = p if side == "yes" else 1 - p
                drift[mode][lab].append(s - s0)
                drift[mode][lab + " vs paid"].append(s - avg)
    print("\n== 2. price drift of what we bought, after entry (side mid; + = moved our way)")
    for mode in ("live", "paper"):
        print(f"   {mode}:")
        for _, lab in HORIZONS:
            d = np.array(drift[mode][lab])
            dp = np.array(drift[mode][lab + " vs paid"])
            if len(d):
                print(f"     {lab:5} n={len(d):4d}  mean {d.mean():+.4f}  median {np.median(d):+.4f}  "
                      f"share falling {100 * np.mean(d < -0.005):3.0f}%  rising "
                      f"{100 * np.mean(d > 0.005):3.0f}%  | vs price paid {dp.mean():+.4f}")


if __name__ == "__main__":
    main()
