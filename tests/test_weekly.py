"""Offline tests for the weekly touch model."""
import math
import unittest
from datetime import datetime

from polyfin.varclock import ET
from polyfin.weekly.discovery import parse_title
from polyfin.weekly.touch import in_session, prob_touch, session_hours_and_opens


def et(*a):
    return datetime(*a, tzinfo=ET).timestamp()


class TestTouch(unittest.TestCase):
    def test_matches_reflection_approximation(self):
        for d, S, H, v in (("up", 100, 101, 1e-4), ("down", 100, 99, 1e-4)):
            approx = 2 * (1 - 0.5 * (1 + math.erf(abs(math.log(H / S)) / math.sqrt(v) / math.sqrt(2))))
            self.assertAlmostEqual(prob_touch(d, S, H, v), approx, delta=0.003)

    def test_limits(self):
        self.assertEqual(prob_touch("up", 100, 99, 1e-4), 1.0)     # already beyond
        self.assertEqual(prob_touch("down", 100, 99, 0.0), 0.0)    # no time left
        self.assertGreater(prob_touch("up", 100, 102, 4e-4), prob_touch("up", 100, 102, 1e-4))

    def test_sessions(self):
        self.assertTrue(in_session(et(2026, 10, 1, 10, 0), "rth"))
        self.assertFalse(in_session(et(2026, 10, 1, 8, 0), "rth"))       # pre-market
        self.assertFalse(in_session(et(2026, 10, 1, 17, 30), "cme"))     # daily break
        self.assertTrue(in_session(et(2026, 10, 1, 18, 30), "cme"))
        self.assertFalse(in_session(et(2026, 10, 3, 12, 0), "fx"))       # Saturday
        self.assertTrue(in_session(et(2026, 10, 4, 17, 30), "fx"))       # Sunday evening
        self.assertEqual(session_hours_and_opens(et(2026, 10, 1, 9, 0), et(2026, 10, 2, 16, 0),
                                                 "rth"), (13.0, 2))

    def test_parse_title(self):
        self.assertEqual(parse_title("↑ $4,250"), ("up", 4250.0))
        self.assertEqual(parse_title("↓ $735"), ("down", 735.0))
        self.assertIsNone(parse_title("$70"))


if __name__ == "__main__":
    unittest.main()


class TestVectorized(unittest.TestCase):
    def test_vec_matches_scalar(self):
        from polyfin.weekly.touch import prob_touch_vec
        cases = [("up", 100, 101, 1e-4), ("down", 100, 99, 3e-4), ("up", 100, 110, 1e-3),
                 ("down", 100, 97, 2e-5), ("up", 100, 99, 1e-4)]
        vec = prob_touch_vec([c[0] == "up" for c in cases], [c[1] for c in cases],
                             [c[2] for c in cases], [c[3] for c in cases])
        for c, pv in zip(cases, vec):
            self.assertAlmostEqual(pv, prob_touch(*c), places=6)
