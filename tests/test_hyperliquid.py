import unittest

from polyfin.hyperliquid import parse_candles


class TestPerps(unittest.TestCase):
    def test_parse_candles(self):
        rows = [{"t": 1791700020000, "o": "93.1", "h": "93.3", "l": "93.0", "c": "93.2", "v": "12.5"},
                {"t": 1791700021000, "o": "1", "h": "1", "l": "1", "c": "1", "v": "1"}]   # not a minute
        self.assertEqual(parse_candles(rows), [(1791700020, 93.1, 93.3, 93.0, 93.2, 12.5)])


if __name__ == "__main__":
    unittest.main()
