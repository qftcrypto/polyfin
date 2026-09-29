"""What the recorder tracks.

Every Polymarket finance *daily* series we record, mapped to the Yahoo symbol
used as its underlying.  Series ids verified against Gamma on 2026-09-29.
`None` for the symbol means there is no free public underlying (SPCX only
exists on Pyth); the Polymarket side is still recorded.

Oracles differ by series (Pyth for stocks/FX/commodities, the WSJ official
close for the cash indices) - see doc/polymarket_daily_market.md.  The Yahoo
symbol is a *predictor*, not the settlement value: futures stand in for spot
metals, NYMEX for Pyth's ICE energy contracts.
"""
from __future__ import annotations

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart"

# series_slug: (series_id, kind, yahoo_symbol)
#   kind: updown   - close vs prior close
#         open     - open vs prior close
#         strikes  - multi-strike "closes above $X" (one market per strike)
SERIES: dict[str, tuple[int, str, str | None]] = {
    # stocks / ETFs (Pyth close)
    "aapl-daily-up-down": (10380, "updown", "AAPL"),
    "msft-daily-up-down": (10379, "updown", "MSFT"),
    "amzn-daily-up-down": (10378, "updown", "AMZN"),
    "googl-daily-up-down": (10377, "updown", "GOOGL"),
    "meta-daily-up-down": (10376, "updown", "META"),
    "tsla-daily-up-down": (10375, "updown", "TSLA"),
    "nvda-daily-up-down": (10374, "updown", "NVDA"),
    "nflx-daily-up-or-down": (10390, "updown", "NFLX"),
    "pltr-daily-up-or-down": (10391, "updown", "PLTR"),
    "open-daily-up-or-down": (10392, "updown", "OPEN"),
    "rklb-daily-up-or-down": (10393, "updown", "RKLB"),
    "abnb-daily-up-or-down": (10394, "updown", "ABNB"),
    "coin-daily-up-or-down": (10943, "updown", "COIN"),
    "hood-daily-up-or-down": (10944, "updown", "HOOD"),
    "mu-daily-up-or-down": (11812, "updown", "MU"),
    "spcx-daily-up-or-down": (11979, "updown", None),
    "spy-daily-up-or-down": (11303, "updown", "SPY"),
    "ewy-daily-up-or-down": (11304, "updown", "EWY"),
    "spy-open-daily-up-or-down": (12335, "open", "SPY"),
    "spy-daily-close-uo": (11525, "strikes", "SPY"),
    "meta-multi-strikes-daily": (12666, "strikes", "META"),
    # commodities (Pyth; futures as proxy)
    "gold-daily-up-or-down": (11307, "updown", "GC=F"),
    "silver-daily-up-or-down": (11308, "updown", "SI=F"),
    "oil-daily-up-or-down": (11309, "updown", "CL=F"),
    "natural-gas-daily-up-or-down": (11311, "updown", "NG=F"),
    "wti-daily-close-uo": (11526, "strikes", "CL=F"),
    # FX (Pyth 1-min candle stamped 4:59 PM ET)
    "eurusd-daily-up-or-down": (10405, "updown", "EURUSD=X"),
    "gbpusd-daily-up-or-down": (10406, "updown", "GBPUSD=X"),
    "usdchf-daily-up-or-down": (10404, "updown", "USDCHF=X"),
    "usdjpy-daily-up-or-down": (10409, "updown", "USDJPY=X"),
    "usdmxn-daily-up-or-down": (10408, "updown", "USDMXN=X"),
    "usdkrw-daily-up-or-down": (11306, "updown", "USDKRW=X"),
    "usdsek-daily-up-or-down": (12087, "updown", "USDSEK=X"),
    "usdnok-daily-up-or-down": (12086, "updown", "USDNOK=X"),
    "usdtry-daily-up-or-down": (12089, "updown", "USDTRY=X"),
    "usdbrl-daily-up-or-down": (12090, "updown", "USDBRL=X"),
    "usdzar-daily-up-or-down": (12088, "updown", "USDZAR=X"),
    "dxy-daily-up-or-down": (12625, "updown", "DX-Y.NYB"),
    # cash indices (WSJ official close)
    "spx-daily-up-or-down": (10383, "updown", "^GSPC"),
    "spx-open-daily-up-or-down": (10945, "open", "^GSPC"),
    "dow-jones-daily-up-or-down": (10384, "updown", "^DJI"),
    "russell-2000-daily-up-or-down": (10388, "updown", "^RUT"),
    "nya": (10395, "updown", "^NYA"),
    "nik-daily-up-or-down": (10382, "updown", "^N225"),
    "ftse-100-daily-up-or-down": (10385, "updown", "^FTSE"),
    "dax-daily-up-or-down": (10386, "updown", "^GDAXI"),
    "hang-seng-daily-up-or-down": (10387, "updown", "^HSI"),
}

# "Opens up or down" compares the open with the prior close, which is the
# settlement of the matching close-to-close series.
OPEN_REF_SERIES = {
    "spy-open-daily-up-or-down": "spy-daily-up-or-down",
    "spx-open-daily-up-or-down": "spx-daily-up-or-down",
}

# Recorded as model features only; no market settles on them.
CONTEXT_SYMBOLS = ["ES=F", "NQ=F", "^VIX", "^NDX"]


def yahoo_symbols() -> list[str]:
    syms = {s for _, _, s in SERIES.values() if s}
    return sorted(syms | set(CONTEXT_SYMBOLS))


LOOKBACK_DAYS = 7            # backfill depth on first run (Yahoo 1m caps at ~8d)

# poll intervals, seconds
BARS_EVERY = 120             # ~50 symbols -> ~1,500 Yahoo req/hr, under the ~2,000 soft cap
BOOKS_EVERY = 30
HISTORY_EVERY = 600
DISCOVERY_EVERY = 900

BOOK_DEPTH = 5               # price levels kept per side
