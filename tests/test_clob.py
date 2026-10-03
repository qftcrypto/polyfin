"""Live executor and reconciliation against a fake CLOB (no network, no wallet).

The fake reproduces the venue behaviours polycrypto hit in production: a FAK
no-match as an HTTP 400 exception, `delayed` responses with zero amounts,
FAKs that rest anyway, FAILED and SELL trades in the history.
"""
import time
import unittest

from polyfin.live.clob import ClobExecutor

NO_MATCH_EXC = Exception("PolyApiException[status_code=400, error_message={'error': "
                         "'no orders found to match with FAK order. FAK orders are "
                         "partially filled or killed if no match is found.'}]")


class FakeClient:
    def __init__(self, post=None, raises=None, orders=(), trades=()):
        self.post, self.raises = post, raises
        self.orders = list(orders)            # successive get_order results
        self.trades = list(trades)
        self.cancelled, self.sent = [], []

    def create_and_post_order(self, args, order_type=None):
        self.sent.append((args.token_id, args.price, args.size, args.side, str(order_type)))
        if self.raises:
            raise self.raises
        return self.post

    def get_order(self, oid):
        return self.orders.pop(0) if self.orders else {"status": "delayed"}

    def cancel_order(self, payload):
        self.cancelled.append(payload.orderID)

    def get_trades(self, params, only_first_page=False):
        if getattr(params, "id", None):
            return [t for t in self.trades if t.get("id") == params.id]
        return [t for t in self.trades if t.get("asset_id") == params.asset_id]


def ex(client):
    return ClobExecutor(client=client, resolve_timeout_s=0.3, poll_s=0.01)


class TestTake(unittest.TestCase):
    def test_sends_a_fak_buy_at_the_limit(self):
        c = FakeClient(post={"success": True, "orderID": "o1", "status": "matched",
                             "makingAmount": "2.1", "takingAmount": "5",
                             "tradeIDs": ["t1"], "transactionsHashes": ["0xabc"]})
        f = ex(c).take("tok", 0.42, 5, [])
        self.assertEqual(c.sent[0][:4], ("tok", 0.42, 5.0, "BUY"))
        self.assertIn("FAK", c.sent[0][4])
        self.assertEqual((f.status, f.shares, f.venue_order_id, f.trade_ids, f.settled),
                         ("filled", 5.0, "o1", ("t1",), True))
        self.assertAlmostEqual(f.avg_price, 0.42)
        self.assertAlmostEqual(f.fee, 5 * 0.04 * 0.42 * 0.58, places=5)

    def test_partial_books_what_matched_not_what_was_asked(self):
        c = FakeClient(post={"success": True, "orderID": "o1", "status": "matched",
                             "makingAmount": "0.8", "takingAmount": "2"})
        f = ex(c).take("tok", 0.40, 5, [])
        self.assertEqual((f.status, f.shares, f.settled), ("partial", 2.0, None))

    def test_no_match_exception_is_a_definite_nofill(self):
        f = ex(FakeClient(raises=NO_MATCH_EXC)).take("tok", 0.4, 5, [])
        self.assertEqual((f.status, f.shares), ("nofill", 0.0))

    def test_min_size_is_a_rejection(self):
        e = Exception("invalid amount for a marketable BUY order ($0.9), min size: $1")
        self.assertEqual(ex(FakeClient(raises=e)).take("tok", 0.18, 5, []).status, "rejected")

    def test_other_exception_is_unknown_not_nofill(self):
        f = ex(FakeClient(raises=TimeoutError("read timed out"))).take("tok", 0.4, 5, [])
        self.assertEqual(f.status, "unknown")

    def test_success_false_is_rejected(self):
        c = FakeClient(post={"success": False, "errorMsg": "not enough balance / allowance"})
        f = ex(c).take("tok", 0.4, 5, [])
        self.assertEqual(f.status, "rejected")
        self.assertIn("allowance", f.error)

    def test_delayed_then_matched(self):
        c = FakeClient(post={"success": True, "orderID": "o9", "status": "delayed",
                             "makingAmount": "0", "takingAmount": "0"},
                       orders=[{"status": "delayed"},
                               {"status": "matched", "size_matched": "5", "price": "0.4"}])
        f = ex(c).take("tok", 0.4, 5, [])
        self.assertEqual((f.status, f.shares), ("filled", 5.0))

    def test_delayed_forever_is_unknown(self):
        c = FakeClient(post={"success": True, "orderID": "o9", "status": "delayed",
                             "makingAmount": "0", "takingAmount": "0"})
        f = ex(c).take("tok", 0.4, 5, [])
        self.assertEqual((f.status, f.venue_order_id), ("unknown", "o9"))

    def test_rested_fak_is_cancelled(self):
        c = FakeClient(post={"success": True, "orderID": "o5", "status": "live",
                             "makingAmount": "0", "takingAmount": "0"})
        f = ex(c).take("tok", 0.4, 5, [])
        self.assertEqual(c.cancelled, ["o5"])
        self.assertEqual(f.status, "nofill")


class TestVenue(unittest.TestCase):
    def setUp(self):
        now = int(time.time())
        self.c = FakeClient(trades=[
            {"id": "a", "asset_id": "tok", "side": "BUY", "size": "3", "price": "0.40",
             "status": "CONFIRMED", "match_time": str(now), "transaction_hash": "0x1"},
            {"id": "b", "asset_id": "tok", "side": "BUY", "size": "2", "price": "0.45",
             "status": "MATCHED", "match_time": str(now)},
            {"id": "c", "asset_id": "tok", "side": "BUY", "size": "9", "price": "0.1",
             "status": "FAILED", "match_time": str(now)},
            {"id": "d", "asset_id": "tok", "side": "SELL", "size": "9", "price": "0.9",
             "status": "CONFIRMED", "match_time": str(now)},
            {"id": "e", "asset_id": "tok", "side": "BUY", "size": "9", "price": "0.9",
             "status": "CONFIRMED", "match_time": str(now - 86400)},
        ])
        self.now = now

    def test_venue_fills_by_order_id(self):
        for t in self.c.trades:
            t["taker_order_id"] = "o2" if t["id"] == "b" else "o1"
        shares, vwap, ids, _ = ex(self.c).venue_fills("tok", since_s=self.now - 60, order_id="o1")
        self.assertEqual((shares, ids), (3.0, ["a"]))     # leg 2's trade (b) is not ours here
        shares, _, ids, _ = ex(self.c).venue_fills("tok", since_s=self.now - 60,
                                                    exclude_orders=["o2"])
        self.assertEqual(ids, ["a"])

    def test_venue_fills_ignores_failed_sells_and_old(self):
        shares, vwap, ids, bps = ex(self.c).venue_fills("tok", since_s=self.now - 60)
        self.assertEqual((shares, sorted(ids)), (5.0, ["a", "b"]))
        self.assertAlmostEqual(vwap, (3 * 0.40 + 2 * 0.45) / 5)

    def test_settlement_state(self):
        e = ex(self.c)
        self.assertTrue(e.settlement_state(["a"]))
        self.assertIsNone(e.settlement_state(["b"]))       # matched, no tx hash yet
        self.assertFalse(e.settlement_state(["c"]))
        self.assertIsNone(e.settlement_state(["zzz"]))     # unknown id: undecided
        self.assertIsNone(e.settlement_state([]))


class FakeClob:
    def __init__(self, fills=None, state=None):
        self.fills, self.state = fills, state

    def venue_fills(self, token_id, since_s=None, order_id=None, exclude_orders=()):
        return None if self.fills is None else (*self.fills, {"0"})

    def settlement_state(self, ids):
        return self.state


class TestReconcileDB(unittest.TestCase):
    """Needs the local Postgres; skipped without it."""

    @classmethod
    def setUpClass(cls):
        try:
            from polyfin.db import connect
            cls.conn = connect()
        except Exception as e:
            raise unittest.SkipTest(f"no database: {e}")
        # reconcile() scans ALL live orders: never run it against a database that
        # has real ones (i.e. production), only a dev database with none
        real = cls.conn.execute("SELECT COUNT(*) FROM trade.orders WHERE mode='live' "
                                "AND condition_id NOT LIKE 'test-%'").fetchone()[0]
        cls.conn.commit()
        if real:
            raise unittest.SkipTest(f"{real} real live orders in this database")

    def _order(self, status, age, shares=0.0, req=5.0):
        cid = f"test-reconcile-{time.time_ns()}"
        oid = self.conn.execute(
            "INSERT INTO trade.orders (created_at, mode, condition_id, series_slug, kind, side,"
            " token_id, target_ts, limit_price, shares_req, status, shares_filled, avg_price)"
            " VALUES (%s,'live',%s,'test','updown','yes','tok',0,0.4,%s,%s,%s,%s) RETURNING id",
            (int(time.time() - age), cid, req, status, shares,
             0.4 if shares else None)).fetchone()[0]
        self.conn.commit()
        self.ids.append(oid)
        return oid

    def _row(self, oid):
        r = self.conn.execute("SELECT status, shares_filled, settle_state, reconciled_at "
                              "FROM trade.orders WHERE id=%s", (oid,)).fetchone()
        self.conn.commit()
        return r

    def setUp(self):
        self.ids = []

    def tearDown(self):
        self.conn.execute("DELETE FROM trade.orders WHERE id = ANY(%s)", (self.ids,))
        self.conn.commit()

    def test_unknown_resolves_to_venue_fill(self):
        from polyfin.live.reconcile import reconcile
        oid = self._order("unknown", age=30)
        reconcile(self.conn, FakeClob(fills=(5.0, 0.41, ["t1"]), state=True))
        st, sh, ss, rec = self._row(oid)
        self.assertEqual((st, sh, ss), ("filled", 5.0, "confirmed"))
        self.assertIsNotNone(rec)

    def test_unknown_stays_unknown_inside_grace(self):
        from polyfin.live.reconcile import reconcile
        oid = self._order("unknown", age=30)
        reconcile(self.conn, FakeClob(fills=(0.0, 0.0, [])))
        self.assertEqual(self._row(oid)[0], "unknown")

    def test_unknown_becomes_nofill_after_grace(self):
        from polyfin.live.reconcile import reconcile
        oid = self._order("unknown", age=400)
        reconcile(self.conn, FakeClob(fills=(0.0, 0.0, [])))
        self.assertEqual(self._row(oid)[:2], ("nofill", 0.0))

    def test_unanswerable_venue_changes_nothing(self):
        from polyfin.live.reconcile import reconcile
        oid = self._order("unknown", age=400)
        reconcile(self.conn, FakeClob(fills=None))
        self.assertEqual(self._row(oid)[0], "unknown")

    def test_failed_settlement_zeroes_the_position(self):
        from polyfin.live.reconcile import reconcile
        oid = self._order("filled", age=400, shares=5.0)
        reconcile(self.conn, FakeClob(fills=(5.0, 0.4, ["t1"]), state=False))
        self.assertEqual(self._row(oid)[:3], ("failed", 0.0, "failed"))


if __name__ == "__main__":
    unittest.main()
