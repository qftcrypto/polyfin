"""Live P&L right now: booked, provisional, and the wallet.

    .venv/bin/python -m polyfin.live.pnl

Booked = settled in trade.orders (the market reached umaResolutionStatus
'resolved' in Gamma).  That lags the real outcome: a stock up/down is decided at
16:00 ET and auto-redeemed into the wallet before Gamma marks it resolved.  So
each unbooked position is also priced from Gamma's current outcome prices:
  resolved   Gamma says resolved, not yet booked by the trader
  decided    past its target and the price is at 0/1 - outcome known, awaiting resolution
  open       still trading
"""
from __future__ import annotations

import json
import time
from collections import defaultdict

from ..db import connect
from ..http import get_json
from ..settings import Credentials


def main() -> None:
    conn = connect()
    booked = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(pnl), 0), COALESCE(SUM(shares_filled * avg_price + fee), 0) "
        "FROM trade.orders WHERE mode = 'live' AND pnl IS NOT NULL").fetchone()
    rows = conn.execute(
        "SELECT condition_id, series_slug, side, leg, shares_filled, avg_price, fee, target_ts "
        "FROM trade.orders WHERE mode = 'live' AND shares_filled > 0 AND settled_at IS NULL "
        "ORDER BY series_slug, leg").fetchall()
    conn.commit()
    cids = sorted({r[0] for r in rows})
    info = {}
    for i in range(0, len(cids), 20):
        q = "&".join(f"condition_ids={c}" for c in cids[i:i + 20])
        for closed in ("false", "true"):          # Gamma returns only open markets by default
            for m in get_json(f"https://gamma-api.polymarket.com/markets?{q}&closed={closed}"
                              f"&limit=50"):
                info[m["conditionId"]] = (json.loads(m.get("outcomePrices") or "[]"),
                                          m.get("umaResolutionStatus"))
    now = time.time()
    tot, n = defaultdict(float), defaultdict(int)
    print(f"{'market':30} {'side':4} {'leg':>3} {'cost':>6} {'yes px':>7} {'state':>9} {'pnl':>7}")
    for cid, slug, side, leg, sh, avg, fee, tgt in rows:
        px, st = info.get(cid, ([], None))
        yes = float(px[0]) if px else None
        cost = sh * avg + fee
        if st == "resolved":
            state, pay = "resolved", yes
        elif tgt < now and yes is not None and (yes > 0.97 or yes < 0.03):
            state, pay = "decided", round(yes)
        else:
            state, pay = "open", None
        pnl = None
        if pay is not None:
            pnl = sh * ((pay if side == "yes" else 1 - pay) - avg) - fee
            tot[state] += pnl
        else:
            tot["open_cost"] += cost
            if yes is not None:                     # mark to market at the current price
                tot["open_mtm"] += sh * ((yes if side == "yes" else 1 - yes) - avg) - fee
        n[state] += 1
        print(f"{slug[:30]:30} {side:4} {leg or 1:3d} {cost:6.2f} "
              f"{yes if yes is not None else float('nan'):7.3f} {state:>9} "
              f"{pnl if pnl is not None else float('nan'):+7.2f}")
    prov = tot["resolved"] + tot["decided"]
    print(f"\nbooked:      {booked[0]} positions, P&L {booked[1]:+.2f} on cost {booked[2]:.2f}")
    print(f"provisional: {n['resolved']} resolved + {n['decided']} decided, P&L {prov:+.2f}")
    print(f"open:        {n['open']} positions, cost {tot['open_cost']:.2f}, "
          f"marked to market {tot['open_mtm']:+.2f}")
    print(f"booked + provisional P&L: {booked[1] + prov:+.2f}   "
          f"(+ open marked to market: {booked[1] + prov + tot['open_mtm']:+.2f})")
    try:
        from .clob import ClobExecutor
        bal = ClobExecutor(Credentials()).collateral_balance()
        print(f"wallet (proxy pUSD): {bal:.2f}  (+ open cost {tot['open_cost']:.2f} "
              f"= {bal + tot['open_cost']:.2f} equity at cost)")
    except Exception as e:
        print(f"wallet unreadable: {e}")


if __name__ == "__main__":
    main()
