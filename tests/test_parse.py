"""Offline tests for the payload parsers:  python3 -m unittest discover tests"""
import json
import unittest

from polyfin.polymarket import iso_ts, parse_book, parse_market, parse_strike
from polyfin.yahoo import parse_chart


class TestParsers(unittest.TestCase):
    def test_iso_ts(self):
        self.assertEqual(iso_ts("2026-09-29T20:00:00Z"), 1790712000)
        # 2-digit fraction (py3.9 fromisoformat rejects it) and short offset
        self.assertEqual(iso_ts("2026-09-28T12:00:59.02+00:00"), 1790596859)
        self.assertEqual(iso_ts("2026-09-28 21:40:45+00"), 1790631645)
        self.assertIsNone(iso_ts(None))

    def test_parse_strike(self):
        self.assertEqual(parse_strike("$795"), 795.0)
        self.assertEqual(parse_strike("$1,234.5"), 1234.5)
        self.assertIsNone(parse_strike(None))

    def test_parse_market_resolved(self):
        m = {"conditionId": "0xabc", "clobTokenIds": json.dumps(["1", "2"]),
             "outcomePrices": json.dumps(["0", "1"]), "closed": True,
             "umaResolutionStatus": "resolved", "endDate": "2026-09-28T20:00:00Z",
             "eventStartTime": "2026-09-28T13:30:00Z", "question": "AAPL Up or Down?"}
        row = parse_market({"slug": "aapl-up-or-down-on-september-28-2026"}, m,
                           "aapl-daily-up-down")
        self.assertEqual((row["token_yes"], row["token_no"]), ("1", "2"))
        self.assertEqual((row["closed"], row["outcome_yes"]), (1, 0.0))
        self.assertEqual(row["symbol"], "AAPL")
        self.assertIsNone(row["strike"])

    def test_parse_market_open_has_no_outcome(self):
        m = {"conditionId": "0xabc", "clobTokenIds": json.dumps(["1", "2"]),
             "outcomePrices": json.dumps(["0.63", "0.37"]), "closed": False,
             "groupItemTitle": "$790", "endDate": "2026-09-29T20:00:00Z"}
        row = parse_market({"slug": "spy-closes-above-on-september-29-2026"}, m,
                           "spy-daily-close-uo")
        self.assertEqual((row["closed"], row["outcome_yes"], row["strike"]), (0, None, 790.0))

    def test_parse_book_best_first(self):
        # CLOB lists bids ascending and asks descending; best is last in both
        b = {"asset_id": "1", "timestamp": "1790656544115",
             "bids": [{"price": "0.40", "size": "390"}, {"price": "0.41", "size": "90"}],
             "asks": [{"price": "0.46", "size": "230"}, {"price": "0.45", "size": "400"}]}
        tok, ts, bb, bs, ba, as_, bids, asks = parse_book(b)
        self.assertEqual((tok, ts, bb, bs, ba, as_), ("1", 1790656544, 0.41, 90.0, 0.45, 400.0))
        self.assertEqual(json.loads(bids)[0], [0.41, 90.0])

    def test_parse_chart_drops_null_close(self):
        p = {"chart": {"result": [{"timestamp": [60, 120],
             "indicators": {"quote": [{"open": [1, 2], "high": [1, 2], "low": [1, 2],
                                       "close": [1.5, None], "volume": [10, 0]}]}}]}}
        self.assertEqual(parse_chart(p), [(60, 1, 1, 1, 1.5, 10)])
        self.assertEqual(parse_chart({"chart": {"result": None}}), [])



class TestBadPrints(unittest.TestCase):
    def test_despike_isolated_bar(self):
        from polyfin.data import Bars
        rows = [(60 * i, 513.0, c) for i, c in enumerate([513.1, 513.4, 480.4, 513.5, 513.6])]
        b = Bars(rows)
        self.assertAlmostEqual(b.close[2], (513.4 + 513.5) / 2)
        self.assertEqual(b.close[1], 513.4)

    def test_real_move_kept(self):
        from polyfin.data import Bars
        rows = [(60 * i, 100.0, c) for i, c in enumerate([100, 100, 103, 103.2, 103.1])]
        self.assertEqual(Bars(rows).close[2], 103)          # neighbours disagree: a move

    def test_latest_price_is_median_of_recent(self):
        from polyfin.data import Bars
        rows = [(60 * i, 513.0, c) for i, c in enumerate([513.1, 513.4, 513.5, 480.4])]
        self.assertEqual(Bars(rows).price_at(60 * 4 + 60), 513.4)   # last print is bad

    def test_partial_minute_bars_dropped(self):
        p = {"chart": {"result": [{"timestamp": [60, 120, 155],
             "indicators": {"quote": [{"open": [1, 2, 3], "high": [1, 2, 3], "low": [1, 2, 3],
                                       "close": [1, 2, 9], "volume": [1, 1, 0]}]}}]}}
        self.assertEqual([r[0] for r in parse_chart(p)], [60, 120])


if __name__ == "__main__":
    unittest.main()
