"""Offline tests for the variance clock and stage 1 pricing."""
import math
import unittest
from datetime import datetime

from polyfin.data import Bars
from polyfin.stage1 import prob_above
from polyfin.varclock import ET, SLOT, VarClock


def et(y, mo, d, h, mi):
    return int(datetime(y, mo, d, h, mi, tzinfo=ET).timestamp())


def session_bars(days, step_ret=0.001, gap_ret=0.01):
    """Regular-hours 1m bars (09:30-15:59 ET) that zig-zag by step_ret and open
    each day gap_ret above the prior close."""
    rows, px = [], 100.0
    for k, (y, mo, d) in enumerate(days):
        if k:
            px *= math.exp(gap_ret)
        t = et(y, mo, d, 9, 30)
        for i in range(390):
            if i:
                px *= math.exp(step_ret if i % 2 else -step_ret)
            rows.append((t + 60 * i, px, px))
    return Bars(rows)


# Mon 2026-09-21 .. Fri 09-25
WEEK = [(2026, 9, 21), (2026, 9, 22), (2026, 9, 23), (2026, 9, 24), (2026, 9, 25)]


class TestProbAbove(unittest.TestCase):
    def test_symmetry_and_limits(self):
        v = 0.01 ** 2
        self.assertAlmostEqual(prob_above(0.0, v), 0.5, delta=0.01)
        self.assertGreater(prob_above(0.02, v), 0.97)
        self.assertLess(prob_above(-0.02, v), 0.03)
        self.assertEqual(prob_above(0.001, 0.0), 1.0)
        self.assertEqual(prob_above(0.0, 0.0), 0.5)


class TestVarClock(unittest.TestCase):
    def setUp(self):
        self.c = VarClock(session_bars(WEEK))

    def test_no_variance_while_closed(self):
        self.assertEqual(self.c.var(et(2026, 9, 24, 17, 0), et(2026, 9, 24, 23, 0)), 0.0)

    def test_session_variance_positive(self):
        self.assertGreater(self.c.var(et(2026, 9, 24, 10, 0), et(2026, 9, 24, 15, 0)), 0.0)

    def test_gap_is_a_jump_at_the_open(self):
        # overnight window ending exactly at the open includes the gap; ending
        # just before it does not
        close, open_ = et(2026, 9, 24, 16, 0), et(2026, 9, 25, 9, 30)
        self.assertAlmostEqual(self.c.var(close, open_), 0.01 ** 2, delta=2e-5)
        self.assertEqual(self.c.var(close, open_ - 60), 0.0)

    def test_additive(self):
        a, m, b = et(2026, 9, 24, 10, 0), et(2026, 9, 24, 12, 7), et(2026, 9, 25, 11, 0)
        self.assertAlmostEqual(self.c.var(a, b), self.c.var(a, m) + self.c.var(m, b), places=12)

    def test_projects_to_next_weekday_not_weekend(self):
        sat = self.c.var(et(2026, 9, 26, 9, 30), et(2026, 9, 26, 16, 0))
        mon = self.c.var(et(2026, 9, 28, 9, 30) + SLOT, et(2026, 9, 28, 16, 0))
        self.assertEqual(sat, 0.0)
        self.assertGreater(mon, 0.0)


if __name__ == "__main__":
    unittest.main()
