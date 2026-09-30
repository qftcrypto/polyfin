"""Live execution on the Polymarket CLOB (py-clob-client-v2, signature type 3).

A synchronous port of polycrypto's `ClobExecutorV2` (polycrypto/live/execution.py),
which has traded this path in production.  What carries over, and why:

* Signature type 3 (POLY_1271) needs py-clob-client-v2: the EOA signs, the
  deposit wallet (`funder`) is maker AND signer, the signature is ERC-7739
  wrapped.  v1 raises "signature type POLY_1271 is not supported".
* FAK only, one shot, no retry: take what rests at or below the limit, kill the
  rest.  polycrypto measured retries of the remainder as a net loss.
* A FAK that matched nothing comes back as an HTTP 400 *exception* whose text
  says so - a definite no-fill, not an error.
* `delayed` with takingAmount 0 is NOT a miss: poll `get_order` until terminal.
  Never book the requested size; book what the venue says matched.
* If the venue rests a FAK anyway (`live` / `unmatched`), cancel it at once.
* Unknown is never "no": an order whose outcome cannot be established is
  recorded `unknown` and reconciled later from the venue's trade history.

Deliberately NOT ported: polycrypto's latency work (client cache seeding,
version priming, keep-warm, per-phase timing).  It bought ~250-600ms for 5m
crypto windows; for daily markets it is noise, and seeding the client's private
caches is the one piece that can silently break on a library upgrade.
"""
from __future__ import annotations

import logging
import time

from .executor import Fill
from .fees import taker_fee

log = logging.getLogger("clob")

HOST = "https://clob.polymarket.com"
CHAIN_ID = 137

#: The venue's words for a FAK that matched nothing, and for a BUY under the $1
#: floor.  Both are definite answers.
NO_MATCH = "no orders found to match with FAK order"
MIN_SIZE = "invalid amount for a marketable BUY order"


def make_client(creds):
    """An L2-authenticated v2 client for the FIN_ wallet."""
    from py_clob_client_v2.client import ClobClient
    from py_clob_client_v2.clob_types import ApiCreds
    if not creds.funder:
        raise ValueError("signature type 3 needs FIN_DEPOSIT_WALLET as funder; without it "
                         "orders are made by the EOA, which holds no collateral")
    return ClobClient(HOST, chain_id=CHAIN_ID, key=creds.private_key,
                      creds=ApiCreds(**creds.api_creds),
                      signature_type=creds.signature_type, funder=creds.funder)


class ClobExecutor:
    mode = "live"

    def __init__(self, creds=None, client=None, resolve_timeout_s: float = 15.0,
                 poll_s: float = 0.25):
        if client is None:
            missing = creds.missing_for_trading
            if missing:
                raise RuntimeError(f"live trading needs {', '.join(missing)} in .env")
            client = make_client(creds)
        self.client = client
        self.resolve_timeout_s = resolve_timeout_s
        self.poll_s = poll_s

    # -- orders -----------------------------------------------------------------
    def take(self, token_id: str, limit: float, shares: float, asks: list,
             label: str = "") -> Fill:
        from py_clob_client_v2.clob_types import OrderArgsV2, OrderType
        args = OrderArgsV2(token_id=token_id, price=float(limit), size=float(shares),
                           side="BUY")
        t0 = time.time()
        try:
            resp = self.client.create_and_post_order(args, order_type=OrderType.FAK)
        except Exception as e:
            msg = str(e)
            if NO_MATCH in msg:
                return Fill("nofill", detail={"venue": "no-match"})
            if MIN_SIZE in msg:
                return Fill("rejected", error="below the venue minimum")
            # The POST raised for another reason: it may or may not have reached
            # the book.  Unknown, reconciled from venue trades later.
            log.error("order failed %s: %s", label, msg[:300])
            return Fill("unknown", error=msg[:500])
        ms = (time.time() - t0) * 1000
        if not isinstance(resp, dict) or not resp.get("success", True):
            err = resp.get("errorMsg") if isinstance(resp, dict) else str(resp)
            log.warning("order REJECTED %s: %s", label, err)
            return Fill("rejected", error=str(err)[:500])

        oid = str(resp.get("orderID") or "")
        status = str(resp.get("status") or "").lower()
        cost = float(resp.get("makingAmount") or 0)
        got = float(resp.get("takingAmount") or 0)

        if got <= 0 and status in ("delayed", "") and oid:
            resolved = self._await_terminal(oid, limit)
            if resolved is None:
                log.warning("UNRESOLVED %s order %s after %.0fs - reconcile later",
                            label, oid, self.resolve_timeout_s)
                return Fill("unknown", venue_order_id=oid, error="unresolved after poll")
            status, cost, got = resolved

        if status in ("live", "unmatched"):
            log.warning("FAK RESTED %s (status %s) - cancelling %s", label, status, oid)
            self._cancel(oid)

        if got <= 0:
            return Fill("nofill", venue_order_id=oid or None)
        price = cost / got
        trade_ids = tuple(resp.get("tradeIDs") or ())
        settled = True if resp.get("transactionsHashes") else None
        log.info("FILLED %s %.2f sh @ %.4f (%.0fms) order %s settle=%s", label, got, price,
                 ms, oid, "confirmed" if settled else "pending")
        return Fill("filled" if got >= shares - 1e-6 else "partial", got, price,
                    # the venue charges per match; the VWAP fee is a slight
                    # over-estimate (p(1-p) is concave) - the safe direction
                    taker_fee(got, price), venue_order_id=oid, trade_ids=trade_ids,
                    settled=settled)

    def _await_terminal(self, oid: str, limit: float):
        """(status, cost, shares) once the order leaves `delayed`, or None."""
        deadline = time.time() + self.resolve_timeout_s
        while time.time() < deadline:
            time.sleep(self.poll_s)
            try:
                o = self.client.get_order(oid)
            except Exception:
                continue
            if not isinstance(o, dict):
                continue
            st = str(o.get("status") or "").lower()
            if st in ("delayed", ""):
                continue
            matched = float(o.get("size_matched") or 0)
            return st, matched * float(o.get("price") or limit), matched
        return None

    def _cancel(self, oid: str) -> None:
        from py_clob_client_v2.clob_types import OrderPayload
        try:
            self.client.cancel_order(OrderPayload(orderID=oid))
        except Exception:
            log.exception("cancel %s failed - an order may be resting", oid)

    # -- reconciliation ---------------------------------------------------------
    def venue_fills(self, token_id: str, since_s: float | None = None):
        """What the VENUE says we bought on this token since `since_s`.

        (shares, vwap, trade ids), shares 0.0 when nothing, or None when the
        question could not be answered - which means unknown, never "no fill".
        """
        from py_clob_client_v2.clob_types import TradeParams
        try:
            rows = [t for t in self.client.get_trades(TradeParams(asset_id=token_id))
                    if str(t.get("status", "")).upper() != "FAILED"
                    and str(t.get("side", "")).upper() == "BUY"
                    and (not since_s or int(t.get("match_time") or 0) >= since_s)]
        except Exception as e:
            log.warning("venue_fills(%s) failed: %s", token_id[:16], str(e)[:120])
            return None
        shares = sum(float(t.get("size") or 0) for t in rows)
        if shares <= 0:
            return 0.0, 0.0, []
        cost = sum(float(t.get("size") or 0) * float(t.get("price") or 0) for t in rows)
        return shares, cost / shares, [t.get("id") for t in rows if t.get("id")]

    def settlement_state(self, trade_ids) -> bool | None:
        """True once any leg has a transaction hash, False if every leg FAILED,
        None while in flight or unknowable."""
        if not trade_ids:
            return None
        from py_clob_client_v2.clob_types import TradeParams
        seen = []
        for tid in trade_ids:
            try:
                seen += [t for t in self.client.get_trades(TradeParams(id=tid),
                                                           only_first_page=True)
                         if t.get("id") == tid]
            except Exception:
                return None
        if len(seen) < len(trade_ids):
            return None
        if all(str(t.get("status", "")).upper() == "FAILED" for t in seen):
            return False
        return True if any(t.get("transaction_hash") for t in seen) else None

    def collateral_balance(self) -> float | None:
        """pUSD in the deposit wallet, per the CLOB (None if unreadable)."""
        from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams
        try:
            ba = self.client.get_balance_allowance(BalanceAllowanceParams(
                asset_type=AssetType.COLLATERAL, signature_type=3))
            return float(ba.get("balance", 0)) / 1e6
        except Exception as e:
            log.warning("balance unreadable: %s", str(e)[:120])
            return None
