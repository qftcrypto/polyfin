"""Order price and size.

Prices are floored to whole cents and sizes are whole shares, so the maker
amount (shares * price) always has <= 2 decimals - the venue rejects a BUY whose
maker amount has more, even on 0.001-tick markets (polycrypto rounding.py).  A
cent is a multiple of every tick in use (0.01, 0.001).
"""
from __future__ import annotations

import math

from .config import BUDGET_USD, MAX_ORDER_USD, MAX_SLIP, MIN_EDGE, MIN_SHARES
from .fees import fee_per_share


def limit_price(p: float, min_edge: float = MIN_EDGE) -> float | None:
    """Highest whole-cent price L with p - L - fee(L) >= min_edge, or None."""
    cents = math.floor((p - min_edge) * 100 + 1e-9)
    while cents >= 1:
        L = cents / 100
        if p - L - fee_per_share(L) >= min_edge - 1e-12:
            return L
        cents -= 1
    return None


def exec_limit(p: float, min_edge: float, best_ask: float,
               max_slip: float = MAX_SLIP) -> float | None:
    """The order limit: the model limit (keeps min_edge after fee), but never above
    best ask + max_slip.  Whole cents, like limit_price."""
    lim = limit_price(p, min_edge)
    if lim is None:
        return None
    return min(lim, math.floor((best_ask + max_slip) * 100 + 1e-9) / 100)


def shares_for(price: float, budget: float = BUDGET_USD) -> int | None:
    """Whole shares for ~budget at price, at least the venue minimum; None if that
    would exceed MAX_ORDER_USD."""
    n = max(MIN_SHARES, math.floor(budget / price + 1e-9))
    return n if n * price <= MAX_ORDER_USD + 1e-9 else None
