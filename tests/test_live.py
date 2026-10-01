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
                         {"base": 0.05, "e10": 0.10, "e15": 0.15, "ladder": 0.05})
        self.assertEqual(C.ARMS["paper"]["ladder"]["rungs"], [0.05, 0.10, 0.15])

    def test_paper_has_no_caps_live_does(self):
        from polyfin.live.engine import blocked
        r = self.risk(early=(500, 5000.0), pnl=-999.0)
        self.assertIsNone(blocked(r, "paper", 3.0, "early"))
        self.assertIsNotNone(blocked(r, "live", 3.0, "early"))

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
        self.assertEqual(blocked(r, "live", 3.0, "early"), "max early orders per day")
        self.assertIsNone(blocked(r, "live", 3.0, "late"))

    def test_slot_and_total_exposure_caps(self):
        from polyfin.live.engine import blocked
        self.assertEqual(blocked(self.risk(late=(1, 38.0)), "live", 3.0, "late"),
                         "max late open exposure")
        self.assertEqual(blocked(self.risk(early=(1, 59.0), late=(1, 39.0)), "live", 3.0,
                                 "late"), "max late open exposure")
        self.assertEqual(blocked(self.risk(pnl=-30.0), "live", 3.0, "late"),
                         "daily loss limit")


class TestLadderDB(unittest.TestCase):
    """Drives Trader._run_arm for the ladder arm against the local Postgres."""

    @classmethod
    def setUpClass(cls):
        try:
            from polyfin.db import connect
            cls.conn = connect()
        except Exception as e:
            raise unittest.SkipTest(f"no database: {e}")

    def setUp(self):
        import time as _t
        from polyfin.live.engine import Trader
        from polyfin.stage1 import Spec
        self.now = _t.time()
        self.cid = f"test-ladder-{_t.time_ns()}"
        self.spec = Spec(self.cid, "test-series", "updown", "TEST", "tokY", int(self.now + 20 * 3600),
                         int(self.now - 3600), None, None, int(self.now + 20 * 3600))
        self.trader = Trader(self.conn, "paper")
        self.cfg = {"min_edge": 0.05, "slots": {"early"}, "rungs": [0.05, 0.10, 0.15]}

    def tearDown(self):
        self.conn.execute("DELETE FROM trade.orders WHERE condition_id = %s", (self.cid,))
        self.conn.commit()

    def cycle(self, p, yes_ask, no_ask):
        books = {"tokY": {"bids": [], "asks": [(yes_ask, 1000.0)], "ts": 0},
                 "tokN": {"bids": [], "asks": [(no_ask, 1000.0)], "ts": 0}}
        self.trader._run_arm("paper", "ladder", self.cfg, [(self.spec, p, (0, 0, 1e-4, 1))],
                             {self.cid: "tokN"}, books, self.now, None)
        return self.conn.execute(
            "SELECT leg, side, round(best_ask::numeric, 2) FROM trade.orders "
            "WHERE condition_id = %s ORDER BY leg", (self.cid,)).fetchall()

    def test_legs_add_on_widening_same_side_only(self):
        from decimal import Decimal as D
        self.assertEqual(self.cycle(0.50, 0.44, 0.60), [(1, "yes", D("0.44"))])   # edge ~0.05
        self.assertEqual(len(self.cycle(0.50, 0.44, 0.60)), 1)                     # no widening
        # the No side now looks far better, but add-ons stay on Yes: nothing (Yes edge 0.05)
        self.assertEqual(len(self.cycle(0.20, 0.44, 0.40)), 1)
        legs = self.cycle(0.50, 0.38, 0.60)                                        # Yes edge ~0.11
        self.assertEqual([(l, sd) for l, sd, _ in legs], [(1, "yes"), (2, "yes")])
        legs = self.cycle(0.50, 0.33, 0.60)                                        # ~0.16
        self.assertEqual([l for l, _, _ in legs], [1, 2, 3])
        self.assertEqual(len(self.cycle(0.50, 0.20, 0.60)), 3)                     # no 4th rung
