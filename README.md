# polyfin

Research and trading for Polymarket **finance daily** markets (stock/ETF, commodity, FX
and index up/downs, plus the SPY/WTI/META "closes above" strikes). Market survey and
oracles: [doc/polymarket_daily_market.md](doc/polymarket_daily_market.md).

## Recorder

Python 3.11 venv (`requirements.txt`), PostgreSQL in Docker on port 5446, no API keys.
Backfills the last 7 days, then keeps recording:

| Table | Source | What | Every |
|---|---|---|---|
| `markets` | Gamma `/events?series_id=` | every market of the 47 series in `polyfin/config.py`, tokens, strike, settlement time, outcome once resolved | 15 min |
| `bars` | Yahoo chart API | 1-minute OHLCV (incl. pre/post) for each underlying + context (`ES=F`, `NQ=F`, `^VIX`, `^NDX`) | 2 min |
| `pm_history` | CLOB `/prices-history` | ~1-minute price of the Up/Yes token | 10 min |
| `pm_books` | CLOB `/books` | top-5 book of the Up/Yes token for unsettled markets | 30 s |

```sh
cp .env.example .env                     # set FIN_PGPASSWORD
docker compose up -d                     # polyfin-timescaledb on 127.0.0.1:5446
uv venv --python 3.11 .venv && uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python -m polyfin.db           # create the schema
.venv/bin/python -m polyfin.recorder     # run forever (--once: one pass)
.venv/bin/python -m unittest discover tests
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
.venv/bin/python -m polyfin.stage1  # price every open market against its latest book
.venv/bin/python -m polyfin.backtest # score resolved markets vs Polymarket (leave-day-out)
```

The backtest scores only markets with >= $500 volume: FX, NYA, HSI, Nikkei, DAX, FTSE and
DXY trade ~$10/day on a 0.01/0.99 book, so there is no market price to compare with.

## Stage 2

`polyfin/stage2.py` adds, on top of stage 1: a **futures nowcast** for US stocks/indices when
their own prints are stale (`S_last · exp(β · r_ES/NQ)`, β from 15m regular-hours returns);
**vol-regime scaling** `v · R^γ` (trailing-6h realized / expected); **sharpening** `Φ(b·d)`;
and an optional **market blend**. Fitted and scored leave-one-day-out.

```sh
.venv/bin/python -m polyfin.stage2         # evaluate, then fit on all days -> data/stage2_params.json
.venv/bin/python -m polyfin.stage2 --live  # price open markets with the saved fit
```

## Trader (paper now, live later)

`polyfin/live/`: one decision loop for paper and live; only the executor differs. Every 30s
it settles resolved positions, reads the control row and risk state from the `trade` schema,
prices open markets of liquid series with stage 2 (sharpening only within 3h of the target),
fetches fresh books for both tokens, and buys the side with the larger edge when
`model_p − ask − fee ≥ 0.05` - a FAK at the highest whole-cent price that keeps that edge,
whole shares, ~$3 (at least the venue's 5 shares, never over $5). One position per market.

Limits (counted from the database, `polyfin/live/config.py`): 30 orders/day, $100 open,
$30 daily realized loss, live pauses itself after 20 fills. Entries stop if bars go stale.

```sh
.venv/bin/python -m polyfin.live.engine           # paper (default); --once for one cycle
.venv/bin/python -m polyfin.live.report           # fill rate, edge, P&L by group
.venv/bin/python -m polyfin.live.control pause "why"   # stop entries; `paper` resumes
```

Live requires both `--live` at launch and `control live` (a ratchet: the row can pause a
process or allow live, never make a paper process live). Live execution
(`polyfin/live/clob.py`) is a port of polycrypto's `ClobExecutorV2`: py-clob-client-v2,
signature type 3, one-shot FAK, venue no-match/`delayed`/rested handling, and nothing is
booked that the venue did not report. `reconcile.py` resolves `unknown` orders from venue
trades, follows on-chain settlement, and redeems winners that auto-redeem missed (relayer).
Wallet setup, preflight and deployment: [deploy/README.md](deploy/README.md).
