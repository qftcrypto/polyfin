"""Live bookkeeping against the venue and the chain.  Runs every cycle, even paused.

Our record of a live order can be wrong in both directions (polycrypto
doc/execution_defects.md): the POST raised or never left `delayed`, so we
booked nothing - but it filled; or we booked a fill whose trade later FAILED on
chain.  So:

1. `unknown`/stale `pending` orders are resolved from the venue's trade history
   for that token.  The venue's trades endpoint lags real fills by 34-79s, so a
   zero only counts as "no fill" after VENUE_GRACE_S; before that it stays
   unknown and keeps its capital reserved.
2. Filled orders are checked once against the venue after the grace period
   (the venue's shares win over ours), then followed until settlement is
   confirmed on chain or FAILED (-> status failed, zero shares).
3. Winners are redeemed.  Polymarket's AutoRedeemOperator normally does it
   unprompted; after REDEEM_FALLBACK_S a still-held winning position is
   redeemed through the relayer.  A zero on-chain balance means auto-redeem
   already did it.
"""
from __future__ import annotations

import json
import logging
import time

from .fees import taker_fee

log = logging.getLogger("reconcile")

VENUE_GRACE_S = 180
PENDING_STALE_S = 60          # a 'pending' row this old means we died mid-order
REDEEM_FALLBACK_S = 900


def reconcile(conn, clob) -> None:
    now = time.time()
    rows = conn.execute(
        "SELECT id, token_id, created_at, status, shares_req, shares_filled, trade_ids, "
        "reconciled_at, venue_order_id FROM trade.orders WHERE mode='live' AND ("
        " status = 'unknown' OR (status = 'pending' AND created_at < %s)"
        " OR (shares_filled > 0 AND reconciled_at IS NULL AND created_at < %s)"
        " OR (shares_filled > 0 AND settle_state IS NULL))",
        (now - PENDING_STALE_S, now - VENUE_GRACE_S)).fetchall()
    conn.commit()
    for oid, tok, created, status, req, filled, tids, rec_at, voi in rows:
        age = now - created
        if status in ("unknown", "pending") or (rec_at is None and age >= VENUE_GRACE_S):
            others = [r[0] for r in conn.execute(
                "SELECT venue_order_id FROM trade.orders WHERE mode='live' AND token_id=%s "
                "AND id <> %s AND venue_order_id IS NOT NULL", (tok, oid))]
            conn.commit()
            v = clob.venue_fills(tok, since_s=created - 5, order_id=voi, exclude_orders=others)
            if v is None:
                continue                                  # unknowable now - stay as is
            shares, vwap, ids, bps = v
            # book the fee the venue charged: fee_rate_bps 0 means none (finance
            # markets, 2026-10-02: all 56 live fills were 0)
            fee = 0.0 if bps == {"0"} else taker_fee(shares, vwap)
            if shares > 0:
                if abs(shares - filled) > 1e-6:
                    log.warning("order %d: venue says %.2f shares, we had %.2f - using venue",
                                oid, shares, filled)
                conn.execute(
                    "UPDATE trade.orders SET status=%s, shares_filled=%s, avg_price=%s, fee=%s,"
                    " trade_ids=%s, filled_at=COALESCE(filled_at, %s), reconciled_at=%s"
                    " WHERE id=%s",
                    ("filled" if shares >= req - 1e-6 else "partial", shares, vwap,
                     fee, json.dumps(ids), int(now), int(now), oid))
                tids = ids
            elif age >= VENUE_GRACE_S:
                if filled > 0:
                    log.error("order %d: we booked %.2f shares, venue has none after %.0fs",
                              oid, filled, age)
                conn.execute("UPDATE trade.orders SET status='nofill', shares_filled=0,"
                             " reconciled_at=%s WHERE id=%s", (int(now), oid))
                conn.commit()
                continue
            conn.commit()
        if tids:
            st = clob.settlement_state(tids if isinstance(tids, list) else json.loads(tids))
            if st is True:
                conn.execute("UPDATE trade.orders SET settle_state='confirmed' WHERE id=%s", (oid,))
            elif st is False:
                log.error("order %d: every trade FAILED on chain - no position", oid)
                conn.execute("UPDATE trade.orders SET settle_state='failed', status='failed',"
                             " shares_filled=0 WHERE id=%s", (oid,))
            conn.commit()


def redeem(conn, chain, relayer, creds) -> None:
    """Redeem settled live winners that auto-redeem has not handled."""
    now = time.time()
    rows = conn.execute(
        "SELECT id, condition_id, token_id, outcome, settled_at FROM trade.orders "
        "WHERE mode='live' AND settled_at IS NOT NULL AND redeemed_at IS NULL "
        "AND shares_filled > 0").fetchall()
    conn.commit()
    for oid, cid, tok, outcome, settled_at in rows:
        if not outcome:
            conn.execute("UPDATE trade.orders SET redeemed_at=%s, redeemed_by='none' "
                         "WHERE id=%s", (int(now), oid))
            conn.commit()
            continue
        if now - settled_at < REDEEM_FALLBACK_S or chain is None:
            continue
        try:
            if not chain.is_resolved(cid):
                continue
            bal = chain.token_balance(tok)
            if bal <= 0:
                by, detail = "auto", "balance 0 - redeemed by AutoRedeemOperator"
            elif relayer is None:
                log.warning("order %d: %.2f winning shares unredeemed and no relayer key",
                            oid, bal)
                continue
            else:
                rec = relayer.redeem(creds.private_key, creds.funder, cid)
                by, detail = "relayer", json.dumps(rec)[:500]
                log.info("order %d: redeemed %.2f shares via relayer", oid, bal)
        except Exception as e:
            log.warning("order %d: redeem check failed: %s", oid, str(e)[:200])
            continue
        conn.execute("UPDATE trade.orders SET redeemed_at=%s, redeemed_by=%s, "
                     "redeem_detail=%s WHERE id=%s", (int(now), by, detail, oid))
        conn.commit()
