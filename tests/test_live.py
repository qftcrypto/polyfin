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


class TestSlots(unittest.TestCase):
    def risk(self, early=(0, 0.0), late=(0, 0.0), pnl=0.0):
        return {"orders_today": early[0] + late[0], "open_usd": early[1] + late[1],
                "pnl_today": pnl, "fills_total": 0,
                "slots": {"early": {"orders_today": early[0], "open_usd": early[1]},
                          "late": {"orders_today": late[0], "open_usd": late[1]}}}

    def test_live_is_base_early_only(self):
        from polyfin.live import config as C
        self.assertEqual(C.ARMS["live"], {"base": {"min_edge": 0.05, "slots": {"early"}}})
        self.assertEqual(C.ARMS["paper"]["base"]["slots"], {"early", "late"})
        self.assertEqual({a: c["min_edge"] for a, c in C.ARMS["paper"].items()},
                         {"base": 0.05, "e10": 0.10, "e15": 0.15})

    def test_higher_arm_limit_is_lower(self):
        # the limit keeps the arm's own edge, so e15 pays at most p - 0.15 - fee
        self.assertEqual(limit_price(0.60, 0.05), 0.54)
        self.assertEqual(limit_price(0.60, 0.15), 0.44)

    def test_slot_boundary(self):
        from polyfin.live.engine import slot_for
        self.assertEqual(slot_for(3 * 3600), "late")
        self.assertEqual(slot_for(3 * 3600 + 1), "early")

    def test_full_early_budget_does_not_block_late(self):
        from polyfin.live.engine import blocked
        r = self.risk(early=(20, 59.0))
        self.assertEqual(blocked(r, "paper", 3.0, "early"), "max early orders per day")
        self.assertIsNone(blocked(r, "paper", 3.0, "late"))

    def test_slot_and_total_exposure_caps(self):
        from polyfin.live.engine import blocked
        self.assertEqual(blocked(self.risk(late=(1, 38.0)), "paper", 3.0, "late"),
                         "max late open exposure")
        self.assertEqual(blocked(self.risk(early=(1, 59.0), late=(1, 39.0)), "paper", 3.0,
                                 "late"), "max late open exposure")
        self.assertEqual(blocked(self.risk(pnl=-30.0), "paper", 3.0, "late"),
                         "daily loss limit")
