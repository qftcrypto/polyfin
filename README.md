# polyfin

Research and trading for Polymarket **finance daily** markets (stock/ETF, commodity, FX
and index up/downs, plus the SPY/WTI/META "closes above" strikes). Market survey and
oracles: [doc/polymarket_daily_market.md](doc/polymarket_daily_market.md).

## Recorder

Stdlib-only Python (3.9+), no keys. Backfills the last 7 days, then keeps recording:

| Table | Source | What | Every |
|---|---|---|---|
| `markets` | Gamma `/events?series_id=` | every market of the 47 series in `polyfin/config.py`, tokens, strike, settlement time, outcome once resolved | 15 min |
| `bars` | Yahoo chart API | 1-minute OHLCV (incl. pre/post) for each underlying + context (`ES=F`, `NQ=F`, `^VIX`, `^NDX`) | 2 min |
| `pm_history` | CLOB `/prices-history` | ~1-minute price of the Up/Yes token | 10 min |
| `pm_books` | CLOB `/books` | top-5 book of the Up/Yes token for unsettled markets | 30 s |

```sh
python3 -m polyfin.recorder              # run forever  -> data/polyfin.sqlite
python3 -m polyfin.recorder --once       # one pass, then exit
python3 -m unittest discover tests       # offline parser tests
```

Restarts are safe: each task fetches only what is missing since the last stored row
(capped at 7 days, Yahoo's 1-minute limit). Timestamps are unix seconds, UTC.

**Skipped underlyings:** SPCX (exists only on Pyth). Its Polymarket side is still recorded.
The Yahoo symbol is a predictor, not the settlement value (futures for spot metals, NYMEX
for Pyth's ICE energy contracts).
