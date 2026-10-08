"""Strikes: repeat-buy while the edge persists, on recorded books only.

    .venv/bin/python -m research.strike_repeat

Model: stage 2 with strikes sharpening, leave-one-day-out, every minute of the
last 36h of each resolved liquid strikes market.  Each minute is matched to the
latest recorded book of the Yes token within 60s (No ladder = 1 - Yes bids);
minutes without one are skipped.  A buy is $3 (>= 5 shares, <= $5) walking the
ladder up to min(model limit keeping edge >= 0.05, ask + 1c).  The side is fixed
by the first buy.  A repeat needs a NEWER book snapshot than the last buy used
(the recorded book never shows our own fills, so reusing a snapshot would buy the
same liquidity twice); refilled depth between snapshots is assumed - optimistic.
"""
from __future__ import annotations

import json
from bisect import bisect_right
from collections import defaultdict

import numpy as np

from polyfin.db import connect
from polyfin.live.fees import fee_per_share
from polyfin.live.sizing import exec_limit, shares_for
from polyfin.stage1 import load_specs
from polyfin.stage2 import Stage2, collect, evaluate

from .fill_repeat import walk


def load_books(conn, tokens):
    lad = defaultdict(lambda: ([], []))
    lo, hi = conn.execute("SELECT MIN(ts), MAX(ts) FROM pm_books").fetchone()
    conn.commit()
    for t0 in range(int(lo), int(hi) + 1, 86400):          # a day per query (512 MB container)
        for tok, ts, bids, asks in conn.execute(
                "SELECT token_id, ts, bids, asks FROM pm_books WHERE ts >= %s AND ts < %s "
                "AND token_id = ANY(%s) ORDER BY token_id, ts", (t0, t0 + 86400, tokens)):
            b = bids if isinstance(bids, list) else json.loads(bids or "[]")
            a = asks if isinstance(asks, list) else json.loads(asks or "[]")
            lad[tok][0].append(ts)
            lad[tok][1].append(([(p, z) for p, z in a], [(round(1 - p, 6), z) for p, z in b]))
        conn.commit()
    return lad


def run(points, gap_s, max_buys, window):
    """points[cid] = chronological [(t, tau, day, p_yes, book_ts, yes_ladder, no_ladder, y)]"""
    fills = []
    for cid, pts in points.items():
        side, last_t, last_book, n = None, -1e18, None, 0
        for t, tau, day, py, bts, ya, na, y in pts:
            if window == "early" and tau <= 3:
                break
            if n >= max_buys:
                break
            sides = {"yes": (py, ya, y), "no": (1 - py, na, 1 - y)}

            def edge(s):
                q, lad, _ = sides[s]
                if not lad or not 0.05 <= lad[0][0] <= 0.97:
                    return None
                return q - lad[0][0] - fee_per_share(lad[0][0])
            if side is None:
                c = [(edge(s), s) for s in sides if edge(s) is not None]
                if not c or max(c)[0] < 0.05:
                    continue
                pick = max(c)[1]
            else:
                pick = side
            e = edge(pick)
            if e is None or e < 0.05 or t - last_t < gap_s or bts == last_book:
                continue
            q, lad, win = sides[pick]
            lim = exec_limit(q, 0.05, lad[0][0])
            sh = shares_for(lim) if lim else None
            if not sh:
                continue
            got, avg = walk(lad, lim, sh)
            if got > 0:
                side, last_t, last_book, n = pick, t, bts, n + 1
                fills.append((cid, day, got, avg, got * (avg + fee_per_share(avg)), win))
    return fills


def show(name, fills):
    if not fills:
        print(f"   {name:34} -")
        return
    byd, bym = defaultdict(float), defaultdict(float)
    cost = defaultdict(float)
    for c, d, got, avg, cst, win in fills:
        byd[d] += got * win - cst
        bym[c] += got * win - cst
        cost[c] += cst
    C, P = sum(f[4] for f in fills), sum(byd.values())
    print(f"   {name:34} {len(fills):5d} {len(bym):4d} {len(fills) / len(bym):5.1f} ${max(cost.values()):6.0f} "
          f"${C:7.0f} {P:+8.2f} {100 * P / C:+6.1f}%  {sum(v > 0 for v in byd.values())}/{len(byd)} "
          f"{P - sum(sorted(bym.values(), reverse=True)[:5]):+8.2f}  "
          + " ".join(f"{d[5:]}:{v:+.0f}" for d, v in sorted(byd.items())))


def main() -> None:
    conn = connect()
    specs = [s for s in load_specs(conn) if s.kind == "strikes"]
    a = collect(Stage2(conn), specs, conn, step=1, hours=36)
    out, _ = evaluate(a)
    p = out["sharp<=3h"]
    sp = {s.condition_id: s for s in specs}
    lad = load_books(conn, list({sp[c].token_yes for c in set(a["cid"])}))
    gaps = [np.median(np.diff(v[0])) for v in lad.values() if len(v[0]) > 10]
    points, skipped = defaultdict(list), 0
    for i in range(len(a["y"])):
        s = sp[a["cid"][i]]
        t = s.target_ts - a["tau"][i] * 3600
        ts, v = lad.get(s.token_yes, ([], []))
        j = bisect_right(ts, t) - 1
        if j < 0 or t - ts[j] > 60:
            skipped += 1
            continue
        points[s.condition_id].append((t, a["tau"][i], a["day"][i], p[i], ts[j], v[j][0], v[j][1], a["y"][i]))
    for c in points:
        points[c].sort()
    days = sorted({q[2] for pts in points.values() for q in pts})
    print(f"strikes: {len(points)} resolved markets with books, days {days[0]}..{days[-1]} ({len(days)}); "
          f"model minutes with a book <= 60s old: {sum(map(len, points.values()))} (skipped {skipped}); "
          f"median book snapshot interval {np.median(gaps):.0f}s\n")
    print(f"   {'rule':34} {'fills':>5} {'mkts':>4} {'f/mkt':>5} {'max$/m':>7} {'cost':>8} {'pnl':>8} "
          f"{'ret/$':>7} days+ {'w/o best5':>8}  by day")
    for window in ("early", "all"):
        print(f"   -- {'early slot (>3h, as live)' if window == 'early' else 'all hours (incl. last 3h)'}")
        show("single entry", run(points, 0, 1, window))
        show("two buys (current ladder2 sizing)*", run(points, 0, 2, window))
        for gap, lab in ((0, "every new snapshot"), (60, "every 1 min"), (300, "every 5 min"),
                         (900, "every 15 min")):
            show(f"repeat {lab}", run(points, gap, 10 ** 9, window))
        for cap in (10, 30):
            show(f"repeat every 1 min, max {cap} buys", run(points, 60, cap, window))
    print("\n   * second buy here repeats at edge >= 0.05; live's second buy waits for 0.10")


if __name__ == "__main__":
    main()
