"""Recorder: backfills the last week, then keeps recording.

    python3 -m polyfin.recorder            # run forever
    python3 -m polyfin.recorder --once     # one pass of every task, then exit

Tasks (intervals in config.py): market discovery, underlying 1m bars, CLOB
price history, CLOB book snapshots.  On first run the bar and history tasks
backfill LOOKBACK_DAYS; afterwards they only fetch what is missing.
"""
from __future__ import annotations

import argparse
import logging
import time

from . import config, polymarket, yahoo
from .db import connect

log = logging.getLogger("recorder")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default=None, help="libpq DSN; default from FIN_PG* in .env")
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    conn = connect(args.dsn)
    symbols = config.yahoo_symbols()

    # order matters on a pass: history and books need discovered markets
    tasks = [
        ("discovery", config.DISCOVERY_EVERY, lambda: polymarket.discover(conn)),
        ("bars", config.BARS_EVERY, lambda: yahoo.record_bars(conn, symbols)),
        ("history", config.HISTORY_EVERY, lambda: polymarket.record_history(conn)),
        ("books", config.BOOKS_EVERY, lambda: polymarket.record_books(conn)),
    ]
    due = {name: 0.0 for name, _, _ in tasks}
    log.info("series=%d  symbols=%d", len(config.SERIES), len(symbols))

    while True:
        for name, every, fn in tasks:
            if time.time() < due[name]:
                continue
            t0 = time.time()
            try:
                n = fn()
                log.info("%-9s %6d rows  %.1fs", name, n, time.time() - t0)
            except Exception:
                log.exception("%s failed", name)
                conn.rollback()     # a failed statement poisons the transaction
            due[name] = t0 + every
        if args.once:
            return
        time.sleep(max(1.0, min(due.values()) - time.time()))


if __name__ == "__main__":
    main()
