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

## Stage 1 model

`P(yes) = Φ((ln(S_now/ref) − v/2) / √v)` - driftless lognormal on a **variance clock**
(`polyfin/varclock.py`): per-symbol expected variance of each 15-minute ET slot, from slot
returns over the recorded week, zero where the asset does not trade, with overnight/weekend
gaps as a jump at the reopen. `ref` is the prior settlement's price (up/down, opens) or the
strike. A market whose reference close is still ahead (tomorrow's up/down) prices off the
variance after that close only, i.e. ~0.5; strikes price off the distance to the strike.

```sh
python3 -m polyfin.stage1       # price every open market against its latest book
python3 -m polyfin.backtest      # score resolved markets vs Polymarket (leave-day-out)
```

The backtest scores only markets with >= $500 volume: FX, NYA, HSI, Nikkei, DAX, FTSE and
DXY trade ~$10/day on a 0.01/0.99 book, so there is no market price to compare with.

## Stage 2

`polyfin/stage2.py` adds, on top of stage 1: a **futures nowcast** for US stocks/indices when
their own prints are stale (`S_last · exp(β · r_ES/NQ)`, β from 15m regular-hours returns);
**vol-regime scaling** `v · R^γ` (trailing-6h realized / expected); **sharpening** `Φ(b·d)`;
and an optional **market blend**. Fitted and scored leave-one-day-out.

```sh
python3 -m polyfin.stage2           # evaluate, then fit on all days -> data/stage2_params.json
python3 -m polyfin.stage2 --live    # price open markets with the saved fit
```
