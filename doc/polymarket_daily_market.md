# Polymarket Finance Daily Markets — Data Sources Report

*Research date: Sept 28, 2026. All markets verified against the live Polymarket Gamma API
(`gamma-api.polymarket.com`). Every URL below was returned by the API or verified via page
open — none invented.*

---

## Part 1 — Stock daily up/down markets: enumeration + free data sources

### Headline correction
The "~30 stocks" estimate is off — **only 18 tickers currently have daily up/down markets**
(verified via the API's `stocks` tag — all 153 tagged events paginated — plus the
`finance-updown` tag, plus targeted probes of 12 additional tickers like AMD, JPM, MSTR, SMCI,
AVGO, ORCL, UNH, DIS, SHOP, PYPL, GME, SKHY, all of which returned nothing). The ~30 figure
likely comes from adjacent markets: 7 index dailies (SPX, NDX, DJI, RUT, UKX, NIK, NYA),
"Opens Up or Down" variants, and FX/commodity up/downs — not stocks.

### Ticker table — all 18
Each ticker has its own daily series (`<ticker>-daily-up-down`). Event URLs follow
`polymarket.com/event/<ticker>-up-or-down-on-<month>-<day>-2026` (verified live; dates rotate daily).

| # | Ticker | Company | Series slug |
|---|---|---|---|
| 1 | AAPL | Apple | `aapl-daily-up-down` |
| 2 | MSFT | Microsoft | `msft-daily-up-down` |
| 3 | AMZN | Amazon | `amzn-daily-up-down` |
| 4 | GOOGL | Alphabet | `googl-daily-up-down` |
| 5 | META | Meta Platforms | `meta-daily-up-down` |
| 6 | TSLA | Tesla | `tsla-daily-up-down` |
| 7 | NVDA | NVIDIA | `nvda-daily-up-down` |
| 8 | NFLX | Netflix | `nflx-daily-up-or-down` |
| 9 | PLTR | Palantir | `pltr-daily-up-or-down` |
| 10 | OPEN | Opendoor | `open-daily-up-or-down` |
| 11 | RKLB | Rocket Lab | `rklb-daily-up-or-down` |
| 12 | ABNB | Airbnb | `abnb-daily-up-or-down` |
| 13 | COIN | Coinbase | `coin-daily-up-or-down` |
| 14 | HOOD | Robinhood | `hood-daily-up-or-down` |
| 15 | MU | Micron | `mu-daily-up-or-down` |
| 16 | **SPCX** ⚠️ | SpaceX — Polymarket synthetic, NOT a listed stock; no public OHLC; resolves via Pyth `Equity.US.SPCX/USD` | `spcx-daily-up-or-down` |
| 17 | **SPY** (ETF) | SPDR S&P 500 ETF (index ETF, not a single stock) | `spy-daily-up-or-down` |
| 18 | **EWY** (ETF) | iShares MSCI South Korea ETF | `ewy-daily-up-or-down` |

15 single stocks + 2 ETFs + 1 synthetic. ("SPY/SPX Opens Up or Down" are a different bet type —
open price, not close — excluded.)

### Resolution detail (critical for modeling)
Every stock market resolves on **Pyth "Close" values** — the Pyth close of the **1-minute candle
at the final minute of regular trading hours**, used *unrounded* (verified from AAPL and SPCX
descriptions; SPX is the exception, resolving on the official index close). Ground truth =
Pyth's equity price feeds; for liquid stocks ≈ the official close, but not identical.

### Data sources

| Source | URL (verified) | Covers | Format / access / limits | Caveats |
|---|---|---|---|---|
| **Yahoo Finance chart API** ✅ primary pick | `https://query1.finance.yahoo.com/v8/finance/chart/{TICKER}?interval=1d&period1=…&period2=…` (verified live, HTTP 200, OHLCV + `adjclose`) | 17/18 — all except SPCX (not a real symbol; errors) | JSON over HTTPS, **no key, no auth**. Undocumented limits (~2,000 req/hr/IP commonly cited; pace 1–2s for bulk) | Train on `close` (unadjusted) to match resolution; `adjclose` only for return calcs. Occasional bad ticks; vs Pyth's final-1-min close differs by ~cents on liquid names — verify edge cases against Pyth |
| **Pyth (resolution ground truth)** ⚠️ | Feed list: `https://benchmarks.pyth.network/v1/price_feeds`; latest: `https://hermes.pyth.network/v2/updates/price/latest?ids[]=<feed_id>`; explorer e.g. `https://pythdata.app/explore/Equity.US.AAPL%2FUSD` (URL verbatim from Polymarket's resolution source) | All 18 **including SPCX** (`Equity.US.SPCX/USD`) — the ONLY source for SPCX | **Tested 2026-09-28:** feed *metadata* is free (`/v2/price_feeds?query=AAPL`, benchmarks `/v1/price_feeds`: 200). **Prices are key-gated**: Hermes latest/by-timestamp and benchmarks `/v1/updates/price/{ts}` → 401 without key, 403 with a bad key; TradingView history shim → 404. AAPL feed id `49f6b65c…5ad55688` | Use for spot-checking labels on ambiguous days (exact-equal closes, halts), not bulk training |
| **Stooq** ❌ currently bot-walled | `https://stooq.com/q/d/l/?s=aapl.us&d1=19900101&d2=20260928&i=d` — **blocked**: returns JS proof-of-work challenge even with browser UA (verified 2026-09-28) | Would cover all US tickers (`aapl.us` style) | Free daily CSV when accessible; scripted access currently fails | Unreliable for automated pipelines right now — use Yahoo |
| **FRED** (fallback, index context) | `https://fred.stlouisfed.org/series/SP500` — free API key required | Index level only (S&P 500 daily; not stocks, not SPY) | CSV/JSON, free key, generous limits | Only relevant if adding SPX/NDX markets later |
| **Nasdaq Data Link** (fallback) | `https://data.nasdaq.com` — free tier needs account; most equity EOD tables premium | US stocks | API key (free signup) | Not needed given Yahoo |

**Recommendation:** Yahoo Finance chart API for bulk daily OHLC on the 15 stocks + 2 ETFs; Pyth
Hermes for spot-verifying edge-day labels; SPCX has no free scripted source — model separately
or exclude.

---
## Part 2 — Other finance daily markets (beyond the stock dailies)

All "Up or Down" = **Pyth** close vs prior trading day's close (ties → 50-50), "exactly as
published by Pyth, without rounding".

| Market | Resolves on | Cadence | URL |
|---|---|---|---|
| Gold Daily Up or Down | Pyth `Metal.XAU/USD` close | 1d | https://polymarket.com/event/xauusd-up-or-down-on-september-28-2026 |
| Silver Daily Up or Down | Pyth `Metal.XAG/USD` close | 1d | https://polymarket.com/event/xagusd-up-or-down-on-september-28-2026 |
| WTI Crude Oil Daily Up or Down | Pyth `CLL` = active-month **ICE Futures Europe WTI** close | 1d | https://polymarket.com/event/wti-up-or-down-on-september-28-2026 |
| Natural Gas Daily Up or Down | Pyth `HN` = active-month **ICE Henry Hub LD1** close | 1d | https://polymarket.com/event/ng-up-or-down-on-september-28-2026 |
| USD/CHF, EUR/USD, GBP/USD, USD/MXN, USD/JPY, USD/KRW Daily Up or Down | Pyth FX feeds; reference = **Close of Pyth 1-min candle stamped 4:59 PM ET** | 1d | https://polymarket.com/event/eurusd-up-or-down-on-september-28-2026 (slugs: `<pair>-up-or-down-on-<date>`) |
| S&P 500 (SPY) closes above $___ | Pyth SPY/USD close; multi-strike Yes/No binaries (e.g. $775–$795) | 1d | https://polymarket.com/event/spy-closes-above-on-september-28-2026 |
| WTI closes above $___ | Pyth CLL close; multi-strike binaries | 1d | https://polymarket.com/event/wti-closes-above-on-september-28-2026 |
| SPY Opens Up or Down | Pyth SPY/USD **open** vs prior close | 1d | https://polymarket.com/event/spy-opens-up-or-down-on-september-28-2026 |
| NYA (NYSE Composite) Up or Down | **Official NYSE Composite closing price** (via WSJ market-data page) | 1d | https://polymarket.com/event/nya-up-or-down-on-september-28-2026 |
| Bitcoin ETF Flows | **Farside Investors** "Total" column (farside.co.uk/btc); Positive/Negative, 0 → 50-50 | 1d | https://polymarket.com/event/bitcoin-etf-flows-on-september-29-2026 |
| Ethereum ETF Flows | Farside Investors (farside.co.uk/eth) | 1d | https://polymarket.com/event/ethereum-etf-flows-on-september-29-2026 |

Notes: index dailies NDX/NIK/SPX/DJI/FTSE/DAX/HSI/RUT also exist (probably inside the known
"~30") — all now resolve on **Pyth**. Copper daily series exists with no markets yet. Retired:
`crude-oil-cl-up-or-down` (Mar 2026), old FX tickers, old commodity tickers
(`xau/xag/xpd/xpt/hg/cl`, brent, RBOB — superseded by the new ones above). **No daily gas-price
market found.**

---
## Part 3 — Free data sources per finance market

### 1. Finance Pyth markets (gold, silver, WTI, nat gas, FX, SPY open, SPY/WTI close-over-under)
- **Stated oracle:** Pyth Network feeds (pythdata.app, e.g. `Equity.US.SPY/USD`, `Metal.XAU/USD`,
  `CLL`, `Commodities.HN`, `FX.EUR/USD`) — "exactly as published by Pyth, without rounding"; FX
  reference = Close of Pyth 1-min candle stamped **4:59 PM ET**; ties → 50-50.
- **Free exact source: N/A (as of Aug 2026).** Verified: `hermes.pyth.network` returns **401
  unauthorized** without `Authorization: Bearer $PYTH_API_KEY` (Pyth docs confirm key now
  required); the old `benchmarks.pyth.network` history API is dead (404). Pyth docs offer "Get
  yours" for a key — free-tier terms unverified; assume key-gated until confirmed.
- **Closest free proxies** (direction usually matches; exact strikes/ties won't):
  - SPY/EWY: Stooq daily CSV `https://stooq.com/q/d/l/?s=spy.us&i=d` (no key) — close can differ
    by cents vs Pyth; fine for direction, not exact U/O strikes.
  - Gold/silver spot: Stooq `xauusd`, `xagusd` daily.
  - WTI: Pyth uses **ICE Futures Europe active-month** — proxy via Stooq WTI futures continuous;
    must replicate "active month" roll timing yourself.
  - Nat gas: Pyth uses **ICE Henry Hub LD1 (HN)** active month — same roll caveat.
  - FX 4:59 PM ET 1-min close: **Dukascopy free tick data** (`datafeed.dukascopy.com`) can
    reconstruct the exact minute; Stooq FX daily as rough proxy.
- **Major caveat:** these markets are only *approximately* modelable without a Pyth key —
  near-the-strike U/O markets and exact ties are unmodelable from proxies.

### 2. NYA (NYSE Composite) daily
- **Stated oracle:** official NYSE Composite closing price, via wsj.com/market-data/stocks
  (paywalled page, but the value is the official close).
- **Free source:** Stooq `^NYA` daily CSV (`https://stooq.com/q/d/l/?s=%5Enya&i=d`, no key) or
  NYSE's own site. Confirm Stooq's ^NYA close matches the official composite close; WSJ itself
  is paywalled.

### 3. BTC/ETH ETF Flows daily
- **Stated oracle:** **Farside Investors** "Total" column at `farside.co.uk/btc/` and
  `farside.co.uk/eth/` (verified live, HTTP 200). Finalized when all providers publish; fallback
  to available data after 12 PM ET +2 days.
- **Free source:** the Farside pages themselves (free to view) — **scrape the HTML table**
  (WordPress site; content also reachable via wp-json page endpoints). **No official API —
  honest N/A** for a clean feed.
- **Caveats:** must match Farside's aggregation exactly ($m rounding); provider figures revise
  after first publication — the market uses Farside's finalized number, not first prints.

### Quick reference — best free endpoints (no key)
- Yahoo Finance chart (stocks/ETFs daily OHLC):
  `https://query1.finance.yahoo.com/v8/finance/chart/{TICKER}?interval=1d&period1=…&period2=…`
- Stooq daily CSV: `https://stooq.com/q/d/l/?s=spy.us&i=d` (⚠️ bot-walled as of 2026-09-28, see Part 1)

### Honest N/A summary
- **Pyth** (all finance dailies): no free exact source — key-gated. Proxies listed above.
- **Farside ETF flows**: no API; scrape required.

### Bottom line — finance trading targets
1. 15 stock + 2 ETF daily up/downs (Pyth oracle; Yahoo close as a close proxy)
2. Gold/silver/WTI/nat-gas/FX/SPY-open dailies and SPY/WTI close-above strikes (Pyth oracle —
   approximately modelable without a key)
3. NYA daily (official NYSE Composite close)
4. BTC/ETH ETF flow dailies (Farside — scrapeable)
