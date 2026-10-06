"""Stop-loss on real entries, against the recorded books that followed them.

    .venv/bin/python -m research.stoploss

Entries: live fills, and paper base/early fills (each judged on its own).  After
each entry, the bought token's book is followed every ~30s (pm_books records the
Yes token; a No position's bid is 1 - Yes ask).  A stop sells ALL shares at the
best bid the first time bid <= entry - X (or < entry/2 for "half"), paying the
taker fee, if that happens before 2 minutes to settlement; otherwise the position
is held to resolution.  'false stops' = stopped positions that would have won.
"""
from __future__ import annotations

import json
from bisect import bisect_right
from collections import defaultdict

import numpy as np

from polyfin.db import connect
from polyfin.live.fees import fee_per_share

STOPS = [("0.05", 0.05), ("0.10", 0.10), ("0.15", 0.15), ("0.20", 0.20), ("0.30", 0.30),
         ("half", None)]


def main() -> None:
    conn = connect()
    entries = conn.execute(
        "SELECT o.mode, o.side, o.created_at, o.target_ts, o.shares_filled, o.avg_price, o.fee, "
        "m.token_yes, m.outcome_yes FROM trade.orders o JOIN markets m USING (condition_id) "
        "WHERE o.shares_filled > 0 AND m.closed = 1 AND m.outcome_yes IS NOT NULL AND "
        "(o.mode = 'live' OR (o.mode = 'paper' AND o.arm = 'base' AND o.slot = 'early'))").fetchall()
    books = defaultdict(lambda: ([], []))
    for tok, ts, bids, asks in conn.execute(
            "SELECT token_id, ts, bids, asks FROM pm_books WHERE token_id = ANY(%s) ORDER BY ts",
            (list({e[7] for e in entries}),)):
        b = bids if isinstance(bids, list) else json.loads(bids or "[]")
        a = asks if isinstance(asks, list) else json.loads(asks or "[]")
        books[tok][0].append(ts)
        books[tok][1].append((b, a))
    conn.commit()

    for mode in ("live", "paper"):
        es = [e for e in entries if e[0] == mode]
        rows = []                       # (hold_pnl, {stop: (pnl, stopped, would_win, depth_ok)})
        for _, side, t0, tgt, sh, px, fee, tok, oy in es:
            win = oy if side == "yes" else 1 - oy
            hold = sh * (win - px) - fee
            ts, snaps = books[tok]
            i = bisect_right(ts, t0)
            path = []
            for t, (b, a) in zip(ts[i:], snaps[i:]):
                if t >= tgt - 120:
                    break
                if side == "yes":
                    bid, size = (b[0][0], b[0][1]) if b else (None, 0)
                else:
                    bid, size = (round(1 - a[0][0], 6), a[0][1]) if a else (None, 0)
                if bid is not None:
                    path.append((bid, size))
            res = {}
            for name, x in STOPS:
                level = px / 2 if x is None else px - x
                hit = next(((bid, size) for bid, size in path if bid <= level), None)
                if hit is None:
                    res[name] = (hold, False, False, True)
                else:
                    bid, size = hit
                    pnl = sh * (bid - px) - fee - sh * fee_per_share(bid)
                    res[name] = (pnl, True, win > 0.5, size >= sh)
            rows.append((hold, res, len(path)))
        covered = [r for r in rows if r[2] > 0]
        hold_all = sum(r[0] for r in covered)
        print(f"== {mode}: {len(es)} settled entries, {len(covered)} with books after entry; "
              f"hold to settlement: P&L {hold_all:+.2f}")
        print(f"   {'stop at':10} {'stopped':>7} {'false stops':>11} {'bid deep':>8} "
              f"{'P&L':>9} {'vs hold':>8}  {'loss saved on true stops':>24} {'profit lost on false':>21}")
        for name, _ in STOPS:
            st = [r for r in covered if r[1][name][1]]
            pnl = sum(r[1][name][0] for r in covered)
            false = [r for r in st if r[1][name][2]]
            saved = sum(r[1][name][0] - r[0] for r in st if not r[1][name][2])
            lost = sum(r[0] - r[1][name][0] for r in false)
            deep = np.mean([r[1][name][3] for r in st]) if st else float("nan")
            print(f"   -{name:9} {len(st):7d} {len(false):5d} ({100 * len(false) / max(len(st), 1):3.0f}%) "
                  f"{100 * deep:7.0f}% {pnl:+9.2f} {pnl - hold_all:+8.2f}  {saved:+24.2f} {-lost:+21.2f}")
        print()


if __name__ == "__main__":
    main()
