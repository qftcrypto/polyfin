"""Weekly touch series we track (Gamma series ids verified 2026-10-01).

Each resolves on Pyth 1-minute candle Highs (up strikes) / Lows (down strikes)
during the week, counting only the series' session:

  rth  stocks/ETFs: Mon-Fri 09:30-16:00 ET (no pre/post market)
  cme  metals and energy: Sun 18:00 - Fri 17:00 ET, daily break 17:00-18:00
  fx   DXY: Sun 17:00 - Fri 17:00 ET, continuous

The Yahoo symbol is a proxy: GC=F/SI=F are futures (Pyth uses spot), NYMEX
CL=F/NG=F stand in for Pyth's ICE contracts.  backtest.py measures each proxy's
offset against resolved strikes instead of assuming it.  SPCX is skipped (no
public underlying).
"""

# series_slug: (series_id, yahoo_symbol, session)
SERIES: dict[str, tuple[int, str, str]] = {
    "spy-hit-price-weekly": (11389, "SPY", "rth"),
    "ewy-hit-price-weekly": (11390, "EWY", "rth"),
    "apple-hit-price-weekly": (11374, "AAPL", "rth"),
    "microsoft-hit-price-weekly": (11375, "MSFT", "rth"),
    "amazon-hit-price-weekly": (11376, "AMZN", "rth"),
    "google-hit-price-weekly": (11377, "GOOGL", "rth"),
    "meta-hit-price-weekly": (11378, "META", "rth"),
    "tesla-hit-price-weekly": (11379, "TSLA", "rth"),
    "nvidia-hit-price-weekly": (11380, "NVDA", "rth"),
    "netflix-hit-price-weekly": (11381, "NFLX", "rth"),
    "palantir-hit-price-weekly": (11382, "PLTR", "rth"),
    "opendoor-hit-price-weekly": (11383, "OPEN", "rth"),
    "rocket-lab-hit-price-weekly": (11384, "RKLB", "rth"),
    "airbnb-hit-price-weekly": (11385, "ABNB", "rth"),
    "coinbase-hit-price-weekly": (11386, "COIN", "rth"),
    "robinhood-hit-price-weekly": (11387, "HOOD", "rth"),
    "micron-hit-price-weekly": (11811, "MU", "rth"),
    "mstr-hit-price-weekly": (12023, "MSTR", "rth"),
    "skhy-hit-price-weekly": (12665, "SKHY", "rth"),
    "gold-hit-price-weekly": (11397, "GC=F", "cme"),
    "silver-hit-price-weekly": (11398, "SI=F", "cme"),
    "wti-crude-oil-hit-price-weekly": (11399, "CL=F", "cme"),
    "natural-gas-hit-price-weekly": (11401, "NG=F", "cme"),
    "dxy-hit-price-weekly": (12626, "DX-Y.NYB", "fx"),
}

# 1m bars the daily recorder does not already take
EXTRA_SYMBOLS = ["MSTR", "SKHY"]

# recorder intervals, seconds
DISCOVERY_EVERY = 900
HISTORY_EVERY = 600
BOOKS_EVERY = 60
BARS_EVERY = 120

BACKFILL_FIDELITY = 30       # minutes per point for resolved weeks
