"""Calm-day filter (paper arm strike2_calm, user 2026-11-05).

A day is calm when an index's last daily close before today (ET) is at or below the
average of the 20 daily closes before that one, e.g. ^OVX (CBOE oil volatility index):
"no trading after the market started expecting a wilder oil market".

The rule was fixed before testing, not fitted: on the 19 live WTI days 10/09-11/04,
OVX above its 20-day average split P&L -$17 vs +$28 (corr -0.14) - too few days to
prove anything, hence a paper test.  Daily closes come from Yahoo's daily chart once
per ET day; unknown (fetch failed, < 21 closes) -> not calm (paper: skip the day).
"""
from __future__ import annotations

import logging
import urllib.parse
from datetime import datetime

from ..config import YAHOO
from ..http import get_json
from ..varclock import ET

log = logging.getLogger("trader")


def daily_closes(symbol: str) -> list[tuple]:
    """[(ET date, close)] from Yahoo's daily chart, oldest first."""
    url = f"{YAHOO}/{urllib.parse.quote(symbol)}?interval=1d&range=3mo"
    r = get_json(url)["chart"]["result"][0]
    closes = r["indicators"]["quote"][0]["close"]
    return [(datetime.fromtimestamp(t, ET).date(), c)
            for t, c in zip(r["timestamp"], closes) if c is not None]


def is_calm(closes: list[tuple], today, days: int = 20) -> tuple[bool, str]:
    prior = [c for d, c in closes if d < today]
    if len(prior) < days + 1:
        return False, f"only {len(prior)} closes"
    last, avg = prior[-1], sum(prior[-days - 1:-1]) / days
    return last <= avg, f"last close {last:.2f} vs {days}d avg {avg:.2f}"


class CalmFilter:
    """Per (symbol, ET day) verdict, fetched once a day."""

    def __init__(self, fetch=daily_closes):
        self.fetch = fetch
        self.cache: dict = {}

    def ok(self, symbol: str, now: float, days: int = 20) -> bool:
        today = datetime.fromtimestamp(now, ET).date()
        key = (symbol, today, days)
        if key not in self.cache:
            try:
                calm, why = is_calm(self.fetch(symbol), today, days)
            except Exception as e:            # retried next cycle, not cached
                log.warning("calm filter: %s fetch failed (%s) - no entries", symbol, e)
                return False
            self.cache = {k: v for k, v in self.cache.items() if k[1] == today}
            self.cache[key] = calm
            log.info("calm filter %s %s: %s -> %s", symbol, today, why,
                     "calm, trading" if calm else "NOT calm, no entries today")
        return self.cache[key]
