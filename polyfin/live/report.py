"""Trading report: fill rate, edge and P&L, overall and by group.

    python -m polyfin.live.report [--mode paper|live] [--days 7]
"""
from __future__ import annotations

import argparse
import time
from collections import defaultdict
from datetime import datetime

from ..db import connect
from ..varclock import ET


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="paper")
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("--arm", default=None, help="one arm only (default: all, split by arm)")
    args = ap.parse_args()
    conn = connect()
    rows = conn.execute(
        "SELECT created_at, series_slug, kind, side, tau_h, model_p, best_ask, edge, "
        "shares_req, status, shares_filled, avg_price, fee, outcome, pnl, slot, arm, leg "
        "FROM trade.orders WHERE mode=%s AND created_at >= %s AND (%s IS NULL OR arm = %s) "
        "ORDER BY created_at",
        (args.mode, time.time() - args.days * 86400, args.arm, args.arm)).fetchall()
    if not rows:
        print(f"no {args.mode} orders in the last {args.days:g} days")
        return

    def summary(name, g):
        tried = len(g)
        filled = [r for r in g if r[10] > 0]
        full = sum(1 for r in g if r[9] == "filled")
        settled = [r for r in filled if r[14] is not None]
        cost = sum(r[10] * r[11] + r[12] for r in filled)
        pnl = sum(r[14] for r in settled)
        wins = sum(1 for r in settled if r[14] > 0)
        edge = sum(r[7] for r in g) / tried
        print(f"   {name:30} {tried:5d} {len(filled):6d} {full / tried:6.0%} {edge:6.3f} "
              f"{cost:8.2f} {len(settled):7d} "
              f"{(wins / len(settled)) if settled else float('nan'):6.0%} {pnl:+8.2f}")

    print(f"== {args.mode} orders, last {args.days:g} days")
    print(f"   {'':30} {'tried':>5} {'filled':>6} {'full%':>6} {'edge':>6} {'cost $':>8} "
          f"{'settled':>7} {'win%':>6} {'pnl $':>8}")
    summary("all", rows)
    g = defaultdict(list)
    for r in rows:
        g[(r[16], r[15])].append(r)
    for (arm, slot), v in sorted(g.items()):
        summary(f"arm={arm} slot={slot}", v)
    g = defaultdict(list)
    for r in rows:
        g[(r[16], r[17])].append(r)
    for (arm, leg), v in sorted(g.items()):
        if any(k[0] == arm and k[1] > 1 for k in g):    # laddered arms only
            summary(f"arm={arm} leg={leg}", v)
    for key, idx in (("arm", 16), ("slot", 15), ("kind", 2), ("side", 3)):
        g = defaultdict(list)
        for r in rows:
            g[r[idx]].append(r)
        for k, v in sorted(g.items()):
            summary(f"{key}={k}", v)
    g = defaultdict(list)
    for r in rows:
        g[datetime.fromtimestamp(r[0], ET).date()].append(r)
    for k, v in sorted(g.items()):
        summary(f"day {k}", v)
    g = defaultdict(list)
    for r in rows:
        g[r[1]].append(r)
    for k, v in sorted(g.items()):
        summary(k[:30], v)


if __name__ == "__main__":
    main()
