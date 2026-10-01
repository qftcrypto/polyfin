"""One-off / re-runnable backfill for the touch backtest.

    python -m polyfin.weekly.backfill

1. every event of every weekly touch series (Gamma keeps them since 2026-03-30)
2. CLOB price history of each market at BACKFILL_FIDELITY minutes
3. Yahoo 1h bars (~730 days) for each proxy symbol -> weekly.bars_1h
Idempotent: re-running only fetches what is missing.
"""
from __future__ import annotations

import logging
import time
import urllib.parse

from ..db import connect
from ..http import get_json
from ..yahoo import parse_chart
from . import config as W
from .discovery import discover, record_history

log = logging.getLogger("weekly-backfill")


def record_bars_1h(conn, symbols) -> int:
    total = 0
    for sym in symbols:
        try:
            p = get_json(f"https://query1.finance.yahoo.com/v8/finance/chart/"
                         f"{urllib.parse.quote(sym)}?interval=1h&range=730d")
        except Exception as e:
            log.warning("%s: %s", sym, e)
            continue
        rows = parse_chart(p)
        conn.executemany(
            "INSERT INTO weekly.bars_1h VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (symbol, ts) "
            "DO UPDATE SET open=excluded.open, high=excluded.high, low=excluded.low, "
            "close=excluded.close, volume=excluded.volume", [(sym, *r) for r in rows])
        conn.commit()
        total += len(rows)
        time.sleep(0.3)
    return total


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    conn = connect()
    t0 = time.time()
    log.info("markets: %d", discover(conn, all_weeks=True))
    log.info("bars_1h: %d", record_bars_1h(conn, sorted({s for _, s, _ in W.SERIES.values()})))
    log.info("history: %d points", record_history(conn, fidelity=W.BACKFILL_FIDELITY))
    n = conn.execute("SELECT COUNT(*), COUNT(DISTINCT event_slug), SUM(closed) "
                     "FROM weekly.markets").fetchone()
    log.info("done in %.0fs: %d markets, %d events, %d resolved", time.time() - t0, *n)


if __name__ == "__main__":
    main()
