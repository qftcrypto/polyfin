"""Weekly paper report:  .venv/bin/python -m polyfin.weekly.report"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from ..db import connect
from ..varclock import ET


def main() -> None:
    conn = connect()
    rows = conn.execute(
        "SELECT arm, event_slug, symbol, direction, strike, status, shares_filled, avg_price, fee, "
        "edge, model_p, best_ask, pnl, end_ts FROM weekly.orders ORDER BY created_at").fetchall()
    if not rows:
        print("no weekly orders yet")
        return
    filled = [r for r in rows if r[6] > 0]
    settled = [r for r in filled if r[12] is not None]
    cost = lambda rs: sum(r[6] * r[7] + r[8] for r in rs)
    print(f"== weekly paper orders: {len(rows)} attempts, {len(filled)} filled "
          f"({sum(r[5] == 'filled' for r in rows)} full), ${cost(filled):.2f} committed, "
          f"avg edge {sum(r[9] for r in filled) / max(len(filled), 1):.3f}")
    if settled:
        pnl = sum(r[12] for r in settled)
        print(f"   settled {len(settled)}: win {sum(r[12] > 0 for r in settled)}, "
              f"pnl ${pnl:+.2f}, return {100 * pnl / cost(settled):+.1f}%")
    print(f"   open: {len(filled) - len(settled)} positions, ${cost([r for r in filled if r[12] is None]):.2f}")
    g = defaultdict(list)
    for r in filled:
        g[(datetime.fromtimestamp(r[13], ET).date(), r[2])].append(r)
    print(f"\n   {'week ending':11} {'symbol':9} {'pos':>4} {'cost$':>7} {'settled':>7} {'pnl$':>7}")
    for (wk, sym), rs in sorted(g.items()):
        st = [r for r in rs if r[12] is not None]
        print(f"   {str(wk):11} {sym:9} {len(rs):4d} {cost(rs):7.2f} {len(st):7d} "
              f"{sum(r[12] for r in st):+7.2f}")


if __name__ == "__main__":
    main()
