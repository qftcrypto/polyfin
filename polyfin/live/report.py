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
    structure(conn, args.mode, time.time() - args.days * 86400)


def structure(conn, mode: str, since: float) -> None:
    """Per arm: how positions are built, how much capital they tie up, and the downside."""
    rows = conn.execute(
        "SELECT arm, condition_id, side, shares_filled, avg_price, fee, filled_at, settled_at, "
        "pnl, target_ts FROM trade.orders WHERE mode = %s AND created_at >= %s "
        "AND shares_filled > 0", (mode, since)).fetchall()
    conn.commit()
    by = defaultdict(list)
    for r in rows:
        by[r[0]].append(r)
    print(f"\n== {mode} position structure by arm (filled orders)")
    print(f"   {'arm':12} {'mkts':>5} {'buys/m':>6} {'max':>4} {'both%':>6} {'pair$':>6} "
          f"{'peak $':>8} {'settled':>7} {'ret/$':>7} {'worst m':>8} {'worst d':>8} {'days+':>6}")
    for arm, rs in sorted(by.items()):
        mk = defaultdict(list)
        for r in rs:
            mk[r[1]].append(r)
        n = [len(v) for v in mk.values()]
        both, pair = 0, []
        for v in mk.values():
            sides = {r[2] for r in v}
            if len(sides) == 2:
                both += 1
                # cost per share on each side (fees included), summed: < 1 locks a profit
                pair.append(sum(sum(r[3] * r[4] + r[5] for r in v if r[2] == sd) /
                                sum(r[3] for r in v if r[2] == sd) for sd in sides))
        ev = sorted([(r[6], r[3] * r[4] + r[5]) for r in rs] +
                    [(r[7], -(r[3] * r[4] + r[5])) for r in rs if r[7]])
        run = peak = 0.0
        for _, d in ev:
            run += d
            peak = max(peak, run)
        st = [r for r in rs if r[8] is not None]
        cost = sum(r[3] * r[4] + r[5] for r in st)
        mnet, dnet = defaultdict(float), defaultdict(float)
        for r in st:
            mnet[r[1]] += r[8]
            dnet[datetime.fromtimestamp(r[9], ET).date()] += r[8]
        print(f"   {arm:12} {len(mk):5d} {sum(n) / len(n):6.1f} {max(n):4d} "
              f"{100 * both / len(mk):5.0f}% "
              f"{(sorted(pair)[len(pair) // 2] if pair else float('nan')):6.3f} {peak:8.2f} "
              f"{len(st):7d} "
              f"{(100 * sum(r[8] for r in st) / cost) if cost else float('nan'):+6.1f}% "
              f"{min(mnet.values(), default=float('nan')):+8.2f} "
              f"{min(dnet.values(), default=float('nan')):+8.2f} "
              f"{sum(v > 0 for v in dnet.values()):3d}/{len(dnet):<2d}")
    print("   both% = markets holding both sides; pair$ = median cost per share Up + Down "
          "(< 1 locks a profit); peak $ = most cost open at once")


if __name__ == "__main__":
    main()
