"""Variance clock: expected log-return variance between two instants.

Per symbol, the squared log return of each 15-minute slot (ET time of day) is
averaged over trading days, giving the expected variance of each slot of a
trading day.  A return across a gap (overnight, weekend, > JUMP_GAP) is a
*jump*: its variance sits as a point mass at the start of the slot that
reopens trading, counted for any interval (t1, t2] containing that instant -
so an "opens up or down" target at exactly 09:30 carries the overnight gap.
Slots in which the asset does not trade carry no variance, so v(t, T) is ~0
once a cash index has closed for the day.

`exclude_day` rebuilds the profile without one ET date - the backtest scores a
market with a profile that has not seen its settlement day.
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

from .data import Bars

ET = ZoneInfo("America/New_York")
SLOT = 15 * 60
NSLOT = 96
MIN_DAY_BARS = 30          # fewer bars than this = not a trading day
MAX_GAP = 4 * 86400        # longer gaps are data holes, not market gaps
MAX_ABS_RET = 0.25         # bad ticks
JUMP_GAP = 30 * 60         # a return across a longer gap is a jump, not diffusion


def _slot(ts: float) -> tuple:
    d = datetime.fromtimestamp(ts, ET)
    return d.date(), d.weekday(), (d.hour * 60 + d.minute) * 60 // SLOT


class VarClock:
    def __init__(self, bars: Bars, horizon_days: int = 4):
        self.day_sumsq: dict = defaultdict(lambda: [0.0] * NSLOT)
        self.day_jump: dict = defaultdict(lambda: [0.0] * NSLOT)
        nbars: dict = defaultdict(int)
        seen: dict = defaultdict(set)          # (weekday, slot) -> dates with bars
        for t in bars.ts:
            day, wd, s = _slot(t)
            nbars[day] += 1
            seen[(wd, s)].add(day)
        self.days = sorted(d for d, n in nbars.items() if n >= MIN_DAY_BARS)

        # One return per traded slot, slot end vs slot start (last close before
        # each boundary).  Summing squared 1m returns instead lets bid/ask bounce
        # in thin pre/post-market trading swamp the estimate (AAPL post-market
        # came out at 3% per session); slot returns pay that noise only twice.
        n, i = len(bars), 0
        t0 = int(bars.ts[0]) // SLOT * SLOT if n else 0
        prev = None                            # (close, bar ts) before this boundary
        for b in range(t0, int(bars.ts[-1]) + SLOT if n else 0, SLOT):
            j = i
            while j < n and bars.ts[j] < b + SLOT - 60:   # bars completed within the slot
                j += 1
            if j > i and prev is not None and b - prev[1] <= MAX_GAP:
                r = math.log(bars.close[j - 1] / prev[0])
                if abs(r) <= MAX_ABS_RET:
                    day, _, s = _slot(b)
                    gap = bars.ts[i] - prev[1] > JUMP_GAP
                    (self.day_jump if gap else self.day_sumsq)[day][s] += r * r
            if j > i:
                prev = (bars.close[j - 1], bars.ts[j - 1])
            i = j

        # A slot trades if bars were seen there on that weekday, or - for Mon-Fri, so
        # that one holiday does not switch a weekday off - in that slot on >= 2 weekdays.
        by_slot: dict = defaultdict(set)
        for (wd, s), ds in seen.items():
            if wd < 5:
                by_slot[s] |= ds
        self.active = {(wd, s) for wd in range(7) for s in range(NSLOT)
                       if seen.get((wd, s)) or (wd < 5 and len(by_slot[s]) >= 2)}

        # a UTC grid of slots from the first bar to `horizon_days` past the last
        if len(bars):
            start = int(bars.ts[0]) // SLOT * SLOT
            end = int(bars.ts[-1]) + horizon_days * 86400
        else:
            start = end = 0
        self.g0 = start
        self.grid = [_slot(t)[1:] for t in range(start, end, SLOT)]
        self._cum: dict = {}

    def profile(self, exclude_day=None, jumps=False) -> list[float]:
        """Expected variance per slot of a trading day (diffusive, or jumps)."""
        src = self.day_jump if jumps else self.day_sumsq
        days = [d for d in self.days if d != exclude_day]
        if not days:
            return [0.0] * NSLOT
        return [sum(src[d][s] for d in days) / len(days) for s in range(NSLOT)]

    def _cumulative(self, exclude_day):
        """(cum, cumj): cum[k] = diffusive variance before slot k; cumj[k] = jump
        variance at the starts of slots < k."""
        if exclude_day not in self._cum:
            prof, jprof = self.profile(exclude_day), self.profile(exclude_day, jumps=True)
            cum, cumj = [0.0], [0.0]
            for wd, s in self.grid:
                on = (wd, s) in self.active
                cum.append(cum[-1] + (prof[s] if on else 0.0))
                cumj.append(cumj[-1] + (jprof[s] if on else 0.0))
            self._cum[exclude_day] = cum, cumj
        return self._cum[exclude_day]

    def _at(self, cums, t: float) -> float:
        """Variance accrued over [grid start, t], jumps at instants <= t included."""
        cum, cumj = cums
        x = (t - self.g0) / SLOT
        if x < 0:
            return 0.0
        k = int(x)
        if k >= len(cum) - 1:
            raise ValueError("time beyond the variance grid; rebuild the clock")
        return cum[k] + (cum[k + 1] - cum[k]) * (x - k) + cumj[k + 1]

    def var(self, t1: float, t2: float, exclude_day=None) -> float:
        """Expected variance of ln S over (t1, t2]."""
        if t2 <= t1:
            return 0.0
        cums = self._cumulative(exclude_day)
        return self._at(cums, t2) - self._at(cums, t1)

    def daily_vol(self, exclude_day=None) -> float:
        """Expected stdev of one full trading day (close-to-close), for display."""
        return math.sqrt(sum(self.profile(exclude_day)) +
                         sum(self.profile(exclude_day, jumps=True)))


def et_date(ts: float):
    return datetime.fromtimestamp(ts, ET).date()

