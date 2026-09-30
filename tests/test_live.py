"""Offline tests for trader sizing, fees, paper fills and the control ratchet."""
import unittest

from polyfin.live.control import RANK
from polyfin.live.executor import PaperExecutor
from polyfin.live.fees import fee_per_share, taker_fee
from polyfin.live.sizing import limit_price, shares_for


class TestSizing(unittest.TestCase):
    def test_limit_keeps_the_edge_after_fee(self):
        L = limit_price(0.90, 0.05)
        self.assertEqual(L, 0.84)                       # 0.85 fails once the fee is counted
        self.assertGreaterEqual(0.90 - L - fee_per_share(L), 0.05)
        self.assertLess(0.90 - (L + 0.01) - fee_per_share(L + 0.01), 0.05)
        self.assertIsNone(limit_price(0.05, 0.05))

    def test_whole_cents_and_shares(self):
        L = limit_price(0.6337, 0.05)
        self.assertAlmostEqual(L * 100, round(L * 100), places=9)

    def test_shares_budget_min_and_cap(self):
        self.assertEqual(shares_for(0.30), 10)          # $3 / 0.30
        self.assertEqual(shares_for(0.84), 5)           # venue minimum: $4.20 > $3
        self.assertIsNone(shares_for(0.97 + 0.04))      # 5 x 1.01 > $5 cap

    def test_fee(self):
        self.assertAlmostEqual(taker_fee(10, 0.5), 0.1)
        self.assertAlmostEqual(fee_per_share(0.85), 0.0051)


class TestPaperExecutor(unittest.TestCase):
    ASKS = [(0.40, 4.0), (0.41, 10.0), (0.45, 100.0)]

    def test_walks_the_ladder_to_the_limit(self):
        f = PaperExecutor().take("t", 0.41, 10, self.ASKS)
        self.assertEqual((f.status, f.shares), ("filled", 10))
        self.assertAlmostEqual(f.avg_price, (4 * 0.40 + 6 * 0.41) / 10)

    def test_partial_and_nofill(self):
        f = PaperExecutor().take("t", 0.40, 10, self.ASKS)
        self.assertEqual((f.status, f.shares), ("partial", 4))
        self.assertEqual(PaperExecutor().take("t", 0.39, 10, self.ASKS).status, "nofill")


class TestControl(unittest.TestCase):
    def test_ratchet_order(self):
        # effective mode = min(launch, row): a paper launch can never go live
        self.assertEqual(min("paper", "live", key=RANK.__getitem__), "paper")
        self.assertEqual(min("live", "paused", key=RANK.__getitem__), "paused")


if __name__ == "__main__":
    unittest.main()
