"""Touch probability: P(the session High/Low reaches a strike before the week ends).

For a driftless price (log drift -v/2), with h = |ln(strike / S)| > 0 and v the
variance left to the end of the window (session hours only):

  up   (High >= H):  P = Phi((-h - v/2)/sqrt v) + e^-h * Phi((-h + v/2)/sqrt v)
  down (Low  <= L):  P = Phi((-h + v/2)/sqrt v) + e^+h * Phi((-h - v/2)/sqrt v)

(reflection principle; both ~ 2 * (1 - Phi(h / sqrt v)) for small v).  Pyth's
1-minute candle highs/lows are effectively continuous monitoring.

Variance comes from hourly bars, never from the future: per-session-hour
variance over the trailing window, times the session hours left, plus one jump
per session reopening left (overnight / weekend gap) at the trailing gap variance.
"""
from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from datetime import datetime

from ..varclock import ET

STEP = 900                     # session accounting resolution, seconds
VOL_WINDOW_DAYS = 28           # trailing calendar days for variance estimates


def phi(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def prob_touch(direction: str, S: float, H: float, v: float) -> float:
    if direction == "up" and S >= H or direction == "down" and S <= H:
        return 1.0
    if v <= 1e-14:
        return 0.0
    h, sv = abs(math.log(H / S)), math.sqrt(v)
    if direction == "up":
        p = phi((-h - v / 2) / sv) + math.exp(-h) * phi((-h + v / 2) / sv)
    else:
        p = phi((-h + v / 2) / sv) + math.exp(h) * phi((-h - v / 2) / sv)
    return min(max(p, 0.0), 1.0)


def in_session(ts: float, session: str) -> bool:
    d = datetime.fromtimestamp(ts, ET)
    wd, hm = d.weekday(), (d.hour, d.minute)
    if session == "rth":
        return wd < 5 and (9, 30) <= hm < (16, 0)
    if session == "cme":               # Sun 18:00 - Fri 17:00, break 17:00-18:00
        if wd == 5 or (wd == 6 and hm < (18, 0)) or (wd == 4 and hm >= (17, 0)):
            return False
        return not ((17, 0) <= hm < (18, 0))
    if session == "fx":                # Sun 17:00 - Fri 17:00, continuous
        return not (wd == 5 or (wd == 6 and hm < (17, 0)) or (wd == 4 and hm >= (17, 0)))
    raise ValueError(session)


def session_hours_and_opens(t1: float, t2: float, session: str) -> tuple[float, int]:
    """(in-session hours in [t1, t2), number of session reopenings in (t1, t2])."""
    hours, opens = 0.0, 0
    t = int(t1) // STEP * STEP
    prev = in_session(t, session)
    while t < t2:
        cur = in_session(t, session)
        if cur:
            hours += (min(t + STEP, t2) - max(t, t1)) / 3600
            if not prev and t > t1:
                opens += 1
        prev = cur
        t += STEP
    return hours, opens


class HourlyVol:
    """Trailing in-session hourly variance and gap variance, from 1h bars."""

    def __init__(self, bars: list[tuple], session: str):
        # bars: (ts, open, high, low, close) ascending
        self.ts, self.close, self.high, self.low = [], [], [], []
        self.ret_ts, self.r2, self.gap_ts, self.g2 = [], [], [], []
        prev = None
        for ts, o, h, lo, c in bars:
            if c is None or not in_session(ts, session):
                continue
            self.ts.append(ts)
            self.close.append(c)
            self.high.append(h)
            self.low.append(lo)
            if prev is not None and c > 0 and prev[1] > 0:
                r2 = math.log(c / prev[1]) ** 2
                if ts - prev[0] <= 3600 * 1.5:
                    self.ret_ts.append(ts)
                    self.r2.append(r2)
                else:                              # across a session break: a gap
                    self.gap_ts.append(ts)
                    self.g2.append(r2)
            prev = (ts, c)
        self._cr = [0.0]
        for x in self.r2:
            self._cr.append(self._cr[-1] + x)
        self._cg = [0.0]
        for x in self.g2:
            self._cg.append(self._cg[-1] + x)

    def _mean(self, ts, cum, t):
        hi = bisect_left(ts, t)                    # strictly before t: no lookahead
        lo = bisect_left(ts, t - VOL_WINDOW_DAYS * 86400)
        n = hi - lo
        return (cum[hi] - cum[lo]) / n if n >= 5 else None

    def var(self, t: float, end: float, session: str) -> float | None:
        h2, g2 = self._mean(self.ret_ts, self._cr, t), self._mean(self.gap_ts, self._cg, t)
        if h2 is None:
            return None
        hours, opens = session_hours_and_opens(t, end, session)
        return hours * h2 + opens * (g2 if g2 is not None else h2)

    def last_close(self, t: float):
        i = bisect_right(self.ts, t - 3600) - 1      # bar complete by t
        return self.close[i] if i >= 0 else None

    def extremes(self, t1: float, t2: float):
        """(max high, min low) of in-session bars completed within [t1, t2]."""
        lo, hi = bisect_left(self.ts, t1), bisect_right(self.ts, t2 - 3600)
        if hi <= lo:
            return None, None
        return max(self.high[lo:hi]), min(self.low[lo:hi])
