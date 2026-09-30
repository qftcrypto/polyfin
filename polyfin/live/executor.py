"""Executors: the only code that differs between paper and live.

Paper is below; live is `clob.ClobExecutor`.  Both take a FAK ("fill and kill")
limit BUY: fill what is available at or below the limit right now, cancel the
rest, never rest on the book.  This is what polycrypto trades with, and it is
what measures fillability directly.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .fees import taker_fee


@dataclass
class Fill:
    status: str                     # filled | partial | nofill | rejected | unknown
    shares: float = 0.0
    avg_price: float | None = None
    fee: float = 0.0
    venue_order_id: str | None = None
    error: str | None = None
    trade_ids: tuple = ()           # CLOB trade ids, for settlement reconciliation
    settled: bool | None = None     # True on-chain confirmed, False failed, None unknown
    detail: dict = field(default_factory=dict)


class PaperExecutor:
    """Fills against the ask ladder fetched for the decision (best first).

    Optimistic by construction: a live FAK reaches the book ~0.6s later (the
    venue holds orders 250ms and re-validates) and competes with other takers.
    Comparing paper and live rows on the same signals measures exactly that gap.
    """
    mode = "paper"

    def take(self, token_id: str, limit: float, shares: float, asks: list,
             label: str = "") -> Fill:
        left, cost = shares, 0.0
        for price, size in asks:
            if price > limit + 1e-9 or left <= 0:
                break
            q = min(left, size)
            cost += q * price
            left -= q
        got = shares - left
        if got <= 0:
            return Fill("nofill")
        avg = cost / got
        return Fill("filled" if left <= 1e-9 else "partial", got, avg, taker_fee(got, avg))
