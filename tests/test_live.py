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

    def test_exec_limit_caps_at_ask_plus_1c(self):
        from polyfin.live.sizing import exec_limit
        self.assertEqual(exec_limit(0.90, 0.05, 0.70), 0.71)   # model allows 0.84
        self.assertEqual(exec_limit(0.60, 0.05, 0.54), 0.54)   # model limit binds first
        self.assertEqual(exec_limit(0.90, 0.05, 0.705), 0.71)  # 0.001-tick ask, whole cents
        self.assertEqual(exec_limit(0.90, 0.05, 0.70, max_slip=0.0), 0.70)

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



class TestSlots(unittest.TestCase):
    def risk(self, early=(0, 0.0), late=(0, 0.0), pnl=0.0):
        return {"orders_today": early[0] + late[0], "open_usd": early[1] + late[1],
                "pnl_today": pnl, "fills_total": 0,
                "slots": {"early": {"orders_today": early[0], "open_usd": early[1]},
                          "late": {"orders_today": late[0], "open_usd": late[1]}}}

    def test_live_is_base_early_only(self):
        from polyfin.live import config as C
        self.assertEqual(C.ARMS["live"], {"ladder2": {"min_edge": 0.05, "slots": {"early"},
                                                      "rungs": [0.05, 0.10]}})
        self.assertEqual(C.LIMITS["live"], {})                 # wallet balance only
        self.assertEqual(C.ARMS["paper"]["base"]["slots"], {"early", "late"})
        self.assertEqual({a: c["min_edge"] for a, c in C.ARMS["paper"].items()},
                         {"base": 0.05, "e10": 0.10, "e15": 0.15, "ladder": 0.05,
                          "rep_hold": 0.05, "rep_flip": 0.05, "rep_flip10": 0.05,
                          "confirm": 0.05, "late_1h": 0.05, "late_blend": 0.05,
                          "min15": 0.05, "ladder2": 0.05,
                          "fav50": 0.05,
                          "strike_rep": 0.05, "strike2": 0.05})
        self.assertEqual({a: C.ARMS["paper"][a]["flip"] for a in ("rep_hold", "rep_flip",
                                                                  "rep_flip10")},
                         {"rep_hold": None, "rep_flip": 0.05, "rep_flip10": 0.10})
        self.assertEqual(C.ARMS["paper"]["ladder"]["rungs"], [0.05, 0.10, 0.15])

    def test_paper_has_no_caps_live_does(self):
        from polyfin.live.engine import blocked
        r = self.risk(early=(500, 5000.0), pnl=-999.0)
        self.assertIsNone(blocked(r, "paper", 3.0, "early"))
        self.assertIsNone(blocked(r, "live", 3.0, "early"))

    def test_higher_arm_limit_is_lower(self):
        # the limit keeps the arm's own edge, so e15 pays at most p - 0.15 - fee
        self.assertEqual(limit_price(0.60, 0.05), 0.54)
        self.assertEqual(limit_price(0.60, 0.15), 0.44)

    def test_slot_boundary(self):
        from polyfin.live.engine import slot_for
        self.assertEqual(slot_for(3 * 3600), "late")
        self.assertEqual(slot_for(3 * 3600 + 1), "early")

    def test_live_has_no_budget_limits(self):
        from polyfin.live.engine import blocked
        # the wallet balance (checked per order in the engine) is the only budget limit
        self.assertIsNone(blocked(self.risk(early=(500, 5000.0), late=(500, 5000.0), pnl=-999.0),
                                  "live", 3.0, "early"))
    def test_paper_has_no_caps_live_does(self):
        from polyfin.live.engine import blocked
        r = self.risk(early=(500, 5000.0), pnl=-999.0)
        self.assertIsNone(blocked(r, "paper", 3.0, "early"))
        self.assertIsNone(blocked(r, "live", 3.0, "early"))

    def test_higher_arm_limit_is_lower(self):
        # the limit keeps the arm's own edge, so e15 pays at most p - 0.15 - fee
        self.assertEqual(limit_price(0.60, 0.05), 0.54)
        self.assertEqual(limit_price(0.60, 0.15), 0.44)

    def test_slot_boundary(self):
        from polyfin.live.engine import slot_for
        self.assertEqual(slot_for(3 * 3600), "late")
        self.assertEqual(slot_for(3 * 3600 + 1), "early")

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

    def test_kinds_filter(self):
        books = {"tokY": {"bids": [], "asks": [(0.30, 1000.0)], "ts": 0},
                 "tokN": {"bids": [], "asks": [(0.95, 1000.0)], "ts": 0}}
        q = "SELECT count(*) FROM trade.orders WHERE arm='kinds_t'"
        for kinds, want in (({"strikes"}, 0), ({"updown"}, 1)):        # the spec is up/down
            cfg = {"min_edge": 0.05, "slots": {"early"}, "kinds": kinds}
            self.trader._run_arm("paper", "kinds_t", cfg, [(self.spec, 0.60, (0, 0, 1e-4, 1))],
                                 {self.cid: "tokN"}, books, self.now, None)
            self.assertEqual(self.conn.execute(q).fetchone()[0], want)
            self.conn.commit()

    def test_min_price_floor(self):
        cfg = {"min_edge": 0.05, "slots": {"early"}, "rungs": [0.05, 0.10], "min_price": 0.15}
        books = lambda ya: {"tokY": {"bids": [], "asks": [(ya, 1000.0)], "ts": 0},
                            "tokN": {"bids": [], "asks": [(0.95, 1000.0)], "ts": 0}}
        run = lambda p, ya: self.trader._run_arm("paper", "min15", cfg, [(self.spec, p, (0, 0, 1e-4, 1))],
                                                 {self.cid: "tokN"}, books(ya), self.now, None)
        q = "SELECT best_ask FROM trade.orders WHERE condition_id=%s AND arm='min15'"
        run(0.20, 0.10)                                   # edge 0.10 but ask below the floor
        self.assertEqual(self.conn.execute(q, (self.cid,)).fetchall(), [])
        run(0.30, 0.20)                                   # above the floor: trades
        self.assertEqual(len(self.conn.execute(q, (self.cid,)).fetchall()), 1)
        self.conn.commit()

    def test_wide_edge_trades(self):
        # no edge cap: a 0.36 edge is traded (bad prints are filtered at load instead)
        self.assertEqual(len(self.cycle(0.90, 0.53, 0.60)), 1)


class TestRepeatDB(TestLadderDB):
    """Repeat arms: spacing, hold vs flip thresholds, buy and dollar caps."""

    def run_arm(self, arm, p, yes_ask, no_ask, at):
        from polyfin.live import config as C
        books = {"tokY": {"bids": [], "asks": [(yes_ask, 1000.0)], "ts": 0},
                 "tokN": {"bids": [], "asks": [(no_ask, 1000.0)], "ts": 0}}
        self.trader._run_repeat_arm("paper", arm, C.ARMS["paper"][arm],
                                    [(self.spec, p, (0, 0, 1e-4, 1))], {self.cid: "tokN"},
                                    books, at, None)
        return [r[0] for r in self.conn.execute(
            "SELECT side FROM trade.orders WHERE condition_id = %s AND arm = %s ORDER BY id",
            (self.cid, arm)).fetchall()]

    def test_spacing_and_hold(self):
        t = self.now
        self.assertEqual(self.run_arm("rep_hold", 0.50, 0.44, 0.60, t), ["yes"])
        self.assertEqual(self.run_arm("rep_hold", 0.50, 0.44, 0.60, t + 60), ["yes"])   # < 15m
        self.assertEqual(self.run_arm("rep_hold", 0.50, 0.44, 0.60, t + 901), ["yes", "yes"])
        # the model now favours No by a wide margin: hold never switches
        self.assertEqual(self.run_arm("rep_hold", 0.20, 0.44, 0.60, t + 1802), ["yes", "yes"])

    def test_flip_needs_the_model_and_the_threshold(self):
        t = self.now
        self.assertEqual(self.run_arm("rep_flip", 0.50, 0.44, 0.60, t), ["yes"])
        self.assertEqual(self.run_arm("rep_flip10", 0.50, 0.44, 0.60, t), ["yes"])
        # No edge ~0.06 (p_no 0.70 vs ask 0.63): rep_flip switches, rep_flip10 does not
        self.assertEqual(self.run_arm("rep_flip", 0.30, 0.80, 0.63, t + 901), ["yes", "no"])
        self.assertEqual(self.run_arm("rep_flip10", 0.30, 0.80, 0.63, t + 901), ["yes"])
        # No edge ~0.11: now rep_flip10 switches too
        self.assertEqual(self.run_arm("rep_flip10", 0.30, 0.80, 0.58, t + 1802), ["yes", "no"])

    def test_buy_and_dollar_caps(self):
        t = self.now
        for k in range(14):                              # 10-buy cap
            sides = self.run_arm("rep_hold", 0.50, 0.30, 0.75, t + k * 901)
        self.assertEqual(len(sides), 10)
        cost = self.conn.execute(
            "SELECT SUM(shares_filled * avg_price + fee) FROM trade.orders "
            "WHERE condition_id = %s AND arm = 'rep_hold'", (self.cid,)).fetchone()[0]
        self.conn.commit()
        self.assertLessEqual(cost, 30.0)
        self.conn.execute("DELETE FROM trade.orders WHERE condition_id = %s", (self.cid,))
        self.conn.commit()
        for k in range(14):                              # $30 cap: 5 shares x 0.80 = $4+ each
            sides = self.run_arm("rep_hold", 0.95, 0.80, 0.25, t + k * 901)
        self.assertEqual(len(sides), 7)                  # 7 x ~$4.03 = $28.2; an 8th > $30


class TestControlAliases(unittest.TestCase):
    def test_pause_alias(self):
        from polyfin.live.control import ALIASES, RANK
        self.assertEqual(ALIASES["pause"], "paused")       # what the docs tell people to type
        self.assertTrue(all(v in RANK for v in ALIASES.values()))



class TestConfirmAndRefreshDB(TestLadderDB):
    def run_at(self, arm, cfg, p, yes_ask, no_ask, at, mode="paper"):
        books = {"tokY": {"bids": [], "asks": [(yes_ask, 1000.0)], "ts": 0},
                 "tokN": {"bids": [], "asks": [(no_ask, 1000.0)], "ts": 0}}
        self.trader._run_arm(mode, arm, cfg, [(self.spec, p, (0, 0, 1e-4, 1))],
                             {self.cid: "tokN"}, books, at, 1e9 if mode == "live" else None)
        return self.conn.execute("SELECT side, best_ask FROM trade.orders WHERE condition_id=%s "
                                 "AND arm=%s ORDER BY id", (self.cid, arm)).fetchall()

    def test_confirm_waits_and_resets(self):
        cfg = {"min_edge": 0.05, "slots": {"early"}, "rungs": [0.05, 0.10], "confirm_s": 120}
        t = self.now
        self.assertEqual(self.run_at("confirm", cfg, 0.50, 0.44, 0.60, t), [])
        self.assertEqual(self.run_at("confirm", cfg, 0.50, 0.44, 0.60, t + 60), [])
        self.assertEqual(self.run_at("confirm", cfg, 0.50, 0.50, 0.60, t + 90), [])   # lapsed
        self.assertEqual(self.run_at("confirm", cfg, 0.50, 0.44, 0.60, t + 121), [])  # restarted
        self.assertEqual(len(self.run_at("confirm", cfg, 0.50, 0.44, 0.60, t + 242)), 1)

    def test_live_refresh_rechecks_the_edge(self):
        from polyfin.live import engine
        from polyfin.live.executor import PaperExecutor
        self.trader.executors["live"] = PaperExecutor()
        cfg = {"min_edge": 0.05, "slots": {"early"}, "rungs": [0.05, 0.10]}
        orig = engine.fetch_books
        try:
            engine.fetch_books = lambda toks: {t: {"bids": [], "asks": [(0.49, 100.0)], "ts": 0}
                                               for t in toks}          # edge gone by send time
            self.assertEqual(self.run_at("t_live", cfg, 0.50, 0.44, 0.60, self.now, "live"), [])
            engine.fetch_books = lambda toks: {t: {"bids": [], "asks": [(0.43, 100.0)], "ts": 0}
                                               for t in toks}          # still there, cheaper
            rows = self.run_at("t_live", cfg, 0.50, 0.44, 0.60, self.now, "live")
            self.assertEqual([(r[0], round(r[1], 2)) for r in rows], [("yes", 0.43)])
        finally:
            engine.fetch_books = orig
            self.conn.execute("DELETE FROM trade.orders WHERE condition_id=%s", (self.cid,))
            self.conn.commit()



class TestLateArmsDB(TestLadderDB):
    def run_late(self, arm, p, yes_bid, yes_ask, no_ask, tau_h):
        from polyfin.live import config as C
        from polyfin.stage1 import Spec
        spec = Spec(self.cid, "test-series", "updown", "TEST", "tokY", int(self.now + tau_h * 3600),
                    int(self.now - 3600), None, None, int(self.now + tau_h * 3600))
        books = {"tokY": {"bids": [(yes_bid, 100.0)], "asks": [(yes_ask, 1000.0)], "ts": 0},
                 "tokN": {"bids": [], "asks": [(no_ask, 1000.0)], "ts": 0}}
        self.trader._run_arm("paper", arm, C.ARMS["paper"][arm], [(spec, p, (0, 0, 1e-4, 1))],
                             {self.cid: "tokN"}, books, self.now, None)
        return self.conn.execute("SELECT side FROM trade.orders WHERE condition_id=%s AND arm=%s",
                                 (self.cid, arm)).fetchall()

    def test_late_1h_only_in_the_last_hour(self):
        self.assertEqual(self.run_late("late_1h", 0.60, 0.48, 0.50, 0.55, 2.0), [])
        self.assertEqual(self.run_late("late_1h", 0.60, 0.48, 0.50, 0.55, 0.5), [("yes",)])

    def test_late_blend_shrinks_toward_the_market(self):
        from polyfin.live.engine import blend_with_market
        pb = blend_with_market(0.80, {"bids": [(0.49, 1)], "asks": [(0.51, 1)]}, (0.19, 0.92))
        self.assertAlmostEqual(pb, 0.565, places=2)
        # raw model 0.80 vs ask 0.51 would trade; blended 0.565 does not clear 0.05 + fee
        self.assertEqual(self.run_late("late_blend", 0.80, 0.49, 0.51, 0.55, 1.5), [])
        # market cheap vs a strong model: blend still finds an edge
        self.assertEqual(self.run_late("late_blend", 0.97, 0.20, 0.22, 0.85, 1.5), [("yes",)])

    def test_late_arms_skip_the_early_slot(self):
        self.assertEqual(self.run_late("late_blend", 0.97, 0.20, 0.22, 0.85, 10.0), [])


if __name__ == "__main__":
    unittest.main()
