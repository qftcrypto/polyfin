"""Read-side helpers over the recorder's tables."""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from statistics import median

SPIKE = 0.01          # a close > 1% from BOTH neighbours that agree with each other is a bad print
RECENT_S = 300        # the latest price is the median of the last 3 bars within 5 minutes


def despike(close: list[float]) -> list[float]:
    """Replace isolated bad prints.  Yahoo's thin after-hours 1m data carries single
    bars 5-7% off (2026-10-01: MSFT 480 against 513 either side), which the model
    read as a move and traded."""
    c = list(close)
    for i in range(1, len(c) - 1):
        a, x, b = c[i - 1], c[i], c[i + 1]
        if a > 0 and b > 0 and abs(x / a - 1) > SPIKE and abs(x / b - 1) > SPIKE \
                and abs(b / a - 1) < SPIKE / 2:
            c[i] = (a + b) / 2
    return c


class Bars:
    """1-minute bars of one symbol; ts = bar open (a bar is complete at ts + 60)."""

    def __init__(self, rows: list[tuple]):
        self.ts = [r[0] for r in rows]
        self.open = [r[1] for r in rows]
        self.close = despike([r[2] for r in rows])

    def _recent(self, i: int) -> float:
        """Median close of the last <= 3 bars ending at i within RECENT_S: one bad
        print at the very end (no right neighbour to despike against) cannot win."""
        j = i
        while j > 0 and i - j < 2 and self.ts[i] - self.ts[j - 1] <= RECENT_S:
            j -= 1
        return median(self.close[j:i + 1])

    def __len__(self):
        return len(self.ts)

    def price_at(self, t: float, max_age: float = 4 * 86400) -> float | None:
        """Close of the last bar completed by t (the settlement convention for a close)."""
        i = bisect_right(self.ts, t - 60) - 1
        if i < 0 or t - self.ts[i] > max_age:
            return None
        return self._recent(i)

    def last_bar(self, t: float) -> tuple[int, float] | None:
        """(ts, close) of the last bar completed by t."""
        i = bisect_right(self.ts, t - 60) - 1
        return (self.ts[i], self._recent(i)) if i >= 0 else None

    def open_at(self, t: float, max_wait: float = 1800) -> float | None:
        """Open of the first bar starting at or after t (an opening print)."""
        i = bisect_left(self.ts, t)
        if i >= len(self.ts) or self.ts[i] - t > max_wait:
            return None
        return self.open[i]


def load_bars(conn, symbol: str) -> Bars:
    return Bars(conn.execute(
        "SELECT ts, open, close FROM bars WHERE symbol=%s AND close > 0 AND ts %% 60 = 0 "
        "ORDER BY ts",
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
        "SELECT ts, p FROM pm_history WHERE token_id=%s ORDER BY ts", (token_id,)).fetchall())
