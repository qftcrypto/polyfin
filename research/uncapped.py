"""What live would have made without its exposure caps.

    .venv/bin/python -m research.uncapped

The paper arms trade live's rules on the same books, uncapped: `base` (early slot)
while live ran the single-entry rule, then `ladder` legs 1-2 once live switched to
the two-buy ladder2 (2026-10-02 02:31 UTC).  A paper trade in a (market, leg) live
never entered is a trade the caps (or timing) kept live out of.  Each is priced
like polyfin.live.pnl: booked, outcome known from Gamma, or marked to market.

Paper fills at the book it saw; a live order arrives ~0.6s later, so this is an
upper bound on what live would have filled.
"""
from __future__ import annotations

import json
import time
from collections import defaultdict
from datetime import datetime, timezone

from polyfin.db import connect
from polyfin.http import get_json

LIVE_START = datetime(2026, 10, 1, 0, 8, tzinfo=timezone.utc).timestamp()
SWITCH = datetime(2026, 10, 2, 2, 31, tzinfo=timezone.utc).timestamp()


def outcomes(cids):
    info = {}
    cids = sorted(cids)
    for i in range(0, len(cids), 20):
        q = "&".join(f"condition_ids={c}" for c in cids[i:i + 20])
        for closed in ("false", "true"):
            for m in get_json(f"https://gamma-api.polymarket.com/markets?{q}&closed={closed}&limit=50"):
                info[m["conditionId"]] = (json.loads(m.get("outcomePrices") or "[]"),
                                          m.get("umaResolutionStatus"))
    return info


def value(rows, info, now):
    """sum of (state -> pnl) for (side, shares, avg, fee, pnl, target_ts, cid) rows"""
    tot, n = defaultdict(float), defaultdict(int)
    for side, sh, avg, fee, pnl, tgt, cid in rows:
        if pnl is not None:
            tot["booked"] += pnl
            n["booked"] += 1
            continue
        px, st = info.get(cid, ([], None))
        yes = float(px[0]) if px else None
        if yes is None:
            n["unpriced"] += 1
            continue
        if st == "resolved" or (tgt < now and (yes > 0.97 or yes < 0.03)):
            pay, k = (yes if st == "resolved" else round(yes)), "decided"
        else:
            pay, k = yes, "open_mtm"
        tot[k] += sh * ((pay if side == "yes" else 1 - pay) - avg) - fee
        n[k] += 1
    return tot, n


def main() -> None:
    conn = connect()
    now = time.time()
    live = conn.execute(
        "SELECT condition_id, COALESCE(leg, 1), side, shares_filled, avg_price, fee, pnl, target_ts "
        "FROM trade.orders WHERE mode='live' AND shares_filled > 0").fetchall()
    live_keys = {(r[0], r[1]) for r in live}
    live_mkts = {r[0] for r in live}
    paper = conn.execute(
        "SELECT condition_id, COALESCE(leg, 1), side, shares_filled, avg_price, fee, pnl, target_ts, "
        "created_at, arm FROM trade.orders WHERE mode='paper' AND shares_filled > 0 AND slot='early' "
        "AND ((arm='base' AND created_at >= %s AND created_at < %s) OR "
        "     (arm='ladder' AND leg <= 2 AND created_at >= %s))",
        (LIVE_START, SWITCH, SWITCH)).fetchall()
    conn.commit()
    missed = []
    for cid, leg, side, sh, avg, fee, pnl, tgt, created, arm in paper:
        key_missing = (cid not in live_mkts) if arm == "base" else ((cid, leg) not in live_keys)
        if key_missing:
            missed.append((side, sh, avg, fee, pnl, tgt, cid, arm, leg))
    info = outcomes({r[0] for r in live} | {m[6] for m in missed})
    lt, ln = value([(r[2], r[3], r[4], r[5], r[6], r[7], r[0]) for r in live], info, now)
    mt, mn = value([m[:7] for m in missed], info, now)
    cost = lambda rs: sum(r[1] * r[2] + r[3] for r in rs)
    tot = lambda t: t["booked"] + t["decided"] + t["open_mtm"]
    print(f"live actual:   {len(live)} positions, cost ${sum(r[3]*r[4]+r[5] for r in live):.2f}: "
          f"booked {lt['booked']:+.2f}, decided {lt['decided']:+.2f}, open at market "
          f"{lt['open_mtm']:+.2f} -> {tot(lt):+.2f}")
    print(f"missed (paper twin trades live never entered): {len(missed)} positions, "
          f"cost ${cost(missed):.2f}  [base window {sum(m[7]=='base' for m in missed)}, "
          f"ladder2 window {sum(m[7]=='ladder' for m in missed)} "
          f"(leg 2: {sum(m[7]=='ladder' and m[8]==2 for m in missed)})]")
    print(f"   booked {mt['booked']:+.2f} ({mn['booked']}), decided {mt['decided']:+.2f} "
          f"({mn['decided']}), open at market {mt['open_mtm']:+.2f} ({mn['open_mtm']}), "
          f"unpriced {mn['unpriced']} -> {tot(mt):+.2f}")
    print(f"\nuncapped live (actual + missed): {tot(lt) + tot(mt):+.2f} on cost "
          f"${sum(r[3]*r[4]+r[5] for r in live) + cost(missed):.2f}")
    by = defaultdict(lambda: [0, 0.0])
    for m in missed:
        d = datetime.fromtimestamp(m[5], timezone.utc).strftime("%m-%d")
        by[d][0] += 1
        by[d][1] += m[1] * m[2] + m[3]
    print("missed by settlement day (count, cost):", {k: (v[0], round(v[1], 2)) for k, v in sorted(by.items())})


if __name__ == "__main__":
    main()
