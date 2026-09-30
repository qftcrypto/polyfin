"""Polymarket taker fee for finance markets.

    fee = shares * rate * (p * (1 - p)) ** exponent,  rate 0.04, exponent 1

taker-only (makers pay nothing, and get a 25% rebate share), added on top of the
notional rather than netted from the shares (see polycrypto/fees.py for how this
was established).  Gamma's makerBaseFee/takerBaseFee = 1000bps is the contract's
ceiling, NOT the charged rate.
"""
from __future__ import annotations

from .config import FEE_RATE


def taker_fee(shares: float, price: float, rate: float = FEE_RATE) -> float:
    return round(shares * rate * price * (1 - price), 5)


def fee_per_share(price: float, rate: float = FEE_RATE) -> float:
    return rate * price * (1 - price)
