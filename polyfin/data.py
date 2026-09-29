"""Read-side helpers over the recorder's tables."""
from __future__ import annotations

from bisect import bisect_left, bisect_right


class Bars:
    """1-minute bars of one symbol; ts = bar open (a bar is complete at ts + 60)."""

    def __init__(self, rows: list[tuple]):
        self.ts = [r[0] for r in rows]
        self.open = [r[1] for r in rows]
        self.close = [r[2] for r in rows]

    def __len__(self):
        return len(self.ts)

    def price_at(self, t: float, max_age: float = 4 * 86400) -> float | None:
        """Close of the last bar completed by t (the settlement convention for a close)."""
        i = bisect_right(self.ts, t - 60) - 1
        if i < 0 or t - self.ts[i] > max_age:
            return None
        return self.close[i]

    def open_at(self, t: float, max_wait: float = 1800) -> float | None:
        """Open of the first bar starting at or after t (an opening print)."""
        i = bisect_left(self.ts, t)
        if i >= len(self.ts) or self.ts[i] - t > max_wait:
            return None
        return self.open[i]


def load_bars(conn, symbol: str) -> Bars:
    return Bars(conn.execute(
        "SELECT ts, open, close FROM bars WHERE symbol=? AND close > 0 ORDER BY ts",
        (symbol,)).fetchall())


class Series:
    """A (ts, p) step series, e.g. a token's prices-history."""

    def __init__(self, rows: list[tuple]):
        self.ts = [r[0] for r in rows]
        self.p = [r[1] for r in rows]

    def at(self, t: float, max_age: float = 1800) -> float | None:
        i = bisect_right(self.ts, t) - 1
        if i < 0 or t - self.ts[i] > max_age:
            return None
        return self.p[i]


def load_history(conn, token_id: str) -> Series:
    return Series(conn.execute(
        "SELECT ts, p FROM pm_history WHERE token_id=? ORDER BY ts", (token_id,)).fetchall())
