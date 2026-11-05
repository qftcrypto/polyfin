"""Fast-market guard (2026-11-05, user): no new entries while the underlying's last hour
moved more than N times its normal hour.

Last-hour vol = sqrt(sum of squared 1-min log returns) over the hour ending at the
newest bar (Yahoo serves CME bars 10-20 min late, so the window ends at the newest bar,
not at now).  Normal = median, over the prior 20 weekdays, of each day's median of that
measure sampled every 30 min 07:00-17:30 ET.  Unknown (too little data) -> no block.

Built after SPY's -4% day on 11-05, when live bought "SPY above X" at model 0.99 vs
market ~0.89 all through the slide.  Test (20 WTI days): the 2x guard almost never fired
on WTI and left P&L unchanged; on 11-05 it would have blocked SPY buys from 09:48 ET.
Insurance, not an edge.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

import numpy as np

from ..varclock import ET

NORMAL_DAYS = 20
MIN_DAYS = 8


def hour_vol(ts: np.ndarray, px: np.ndarray, end: float) -> float:
    """Realised vol of 1-min log returns over (end - 1h, end], from the last bar at or
    before end - 1h (nan if no bar inside the hour)."""
    j = int(np.searchsorted(ts, end, side="right")) - 1
    i = max(int(np.searchsorted(ts, end - 3600, side="right")) - 1, 0)
    if j <= i:
        return math.nan
    r = np.diff(np.log(px[i:j + 1]))
    return float(np.sqrt(np.sum(r * r)))


def normal_hour_vol(ts: np.ndarray, px: np.ndarray, today) -> float:
    """Median over the prior NORMAL_DAYS weekdays of each day's median hour vol."""
    meds, d = [], today
    while len(meds) < NORMAL_DAYS:
        d -= timedelta(days=1)
        if d.weekday() >= 5:
            continue
        if d < today - timedelta(days=45):
            break
        v = [hour_vol(ts, px, datetime(d.year, d.month, d.day, h, m, tzinfo=ET).timestamp())
             for h in range(7, 18) for m in (0, 30)]
        v = [x for x in v if not math.isnan(x)]
        meds.append(float(np.median(v)) if v else math.nan)
    meds = [x for x in meds if not math.isnan(x)]
    return float(np.median(meds)) if len(meds) >= MIN_DAYS else math.nan


class SpeedGuard:
    """Per-symbol last-hour vol / normal, normals cached per ET day."""

    def __init__(self):
        self.normals: dict = {}

    def _bars(self, conn, symbol: str, since: float):
        rows = conn.execute("SELECT ts, close FROM bars WHERE symbol = %s AND ts >= %s "
                            "AND close > 0 ORDER BY ts", (symbol, int(since))).fetchall()
        conn.commit()
        if not rows:
            return np.array([]), np.array([])
        a = np.array(rows, dtype=float)
        return a[:, 0], a[:, 1]

    def ratio(self, conn, symbol: str, now: float) -> float:
        today = datetime.fromtimestamp(now, ET).date()
        key = (symbol, today)
        if key not in self.normals:
            ts, px = self._bars(conn, symbol, now - 46 * 86400)
            self.normals = {k: v for k, v in self.normals.items() if k[1] == today}
            self.normals[key] = normal_hour_vol(ts, px, today) if len(ts) else math.nan
        normal = self.normals[key]
        if not normal > 0:
            return math.nan
        ts, px = self._bars(conn, symbol, now - 3 * 3600)
        if len(ts) < 2:
            return math.nan
        return hour_vol(ts, px, ts[-1]) / normal
