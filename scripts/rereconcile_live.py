#!/usr/bin/env python3
"""Re-check every live fill against the venue, by venue order id, and fix the records.

    .venv/bin/python scripts/rereconcile_live.py            # show differences
    .venv/bin/python scripts/rereconcile_live.py --write    # apply them

One-off repair for the 2026-10-02 reconcile bug (a ladder's leg 1 was credited
leg 2's shares on the same token).  Fees are the published taker fee: the trade
records' fee_rate_bps reads 0, but on-chain the wallet paid ~$4.11 above trade
notional on 56 fills, matching the estimate.  Idempotent.
P&L of settled orders is recomputed from the corrected shares, price and fee.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from polyfin.db import connect  # noqa: E402
from polyfin.live.clob import ClobExecutor  # noqa: E402
from polyfin.live.fees import taker_fee  # noqa: E402
from polyfin.settings import Credentials  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args()
    conn = connect()
    clob = ClobExecutor(Credentials())
    rows = conn.execute(
        "SELECT id, token_id, created_at, shares_filled, avg_price, fee, venue_order_id, outcome "
        "FROM trade.orders WHERE mode='live' AND shares_filled > 0 ORDER BY id").fetchall()
    conn.commit()
    changed = 0
    for oid, tok, created, sh, px, fee, voi, outcome in rows:
        if not voi:
            print(f"   {oid}: no venue order id - left as is")
            continue
        v = clob.venue_fills(tok, since_s=created - 5, order_id=voi)
        if v is None or v[0] <= 0:
            print(f"   {oid}: venue answer {v} - left as is")
            continue
        vsh, vpx, ids, bps = v
        vfee = taker_fee(vsh, vpx)        # trade records' fee_rate_bps (0) is not the fee
        if abs(vsh - sh) > 1e-6 or abs(vpx - px) > 1e-6 or abs(vfee - fee) > 1e-6:
            changed += 1
            pnl = vsh * (outcome - vpx) - vfee if outcome is not None else None
            print(f"   {oid}: {sh:.2f}@{px:.4f} fee {fee:.4f} -> {vsh:.2f}@{vpx:.4f} fee {vfee:.4f}"
                  + (f", pnl {pnl:+.2f}" if pnl is not None else ""))
            if a.write:
                conn.execute("UPDATE trade.orders SET shares_filled=%s, avg_price=%s, fee=%s, "
                             "pnl=COALESCE(%s, pnl) WHERE id=%s", (vsh, vpx, vfee, pnl, oid))
    if a.write:
        conn.commit()
    print(f"{changed} orders {'fixed' if a.write else 'would change (re-run with --write)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
