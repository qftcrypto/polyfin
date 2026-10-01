"""Weekly recorder: touch markets, their 1m price history and books, extra 1m bars.

    python -m polyfin.weekly.recorder            # run forever (deploy/polyfin-weekly.service)
    python -m polyfin.weekly.recorder --once
"""
from __future__ import annotations

import argparse
import logging
import time

from .. import config as mcfg
from .. import yahoo
from ..db import connect
from ..http import get_json
from ..polymarket import parse_book
from . import config as W
from .backfill import record_bars_1h
from .discovery import discover, record_history

log = logging.getLogger("weekly-recorder")


def record_books(conn) -> int:
    toks = [r[0] for r in conn.execute(
        "SELECT token_yes FROM weekly.markets WHERE closed = 0 AND end_ts > %s",
        (int(time.time()),))]
    conn.commit()
    total = 0
    for i in range(0, len(toks), 50):
        try:
            books = get_json(f"{mcfg.CLOB}/books", body=[{"token_id": t} for t in toks[i:i + 50]])
        except Exception as e:
            log.warning("books: %s", e)
            continue
        conn.executemany("INSERT INTO pm_books VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
                         "ON CONFLICT DO NOTHING",
                         [parse_book(b) for b in books if b.get("asset_id")])
        conn.commit()
        total += len(books)
    return total


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    conn = connect()
    tasks = [
        ("discovery", W.DISCOVERY_EVERY, lambda: discover(conn)),
        ("history", W.HISTORY_EVERY, lambda: record_history(conn, fidelity=1)),
        ("books", W.BOOKS_EVERY, lambda: record_books(conn)),
        ("bars", W.BARS_EVERY, lambda: yahoo.record_bars(conn, W.bar_symbols())),
        ("bars_1h", W.BARS_1H_EVERY, lambda: record_bars_1h(conn, W.bar_symbols(), "5d")),
    ]
    due = {n: 0.0 for n, _, _ in tasks}
    log.info("weekly series=%d", len(W.SERIES))
    while True:
        for name, every, fn in tasks:
            if time.time() < due[name]:
                continue
            t0 = time.time()
            try:
                log.info("%-9s %6d rows  %.1fs", name, fn(), time.time() - t0)
            except Exception:
                log.exception("%s failed", name)
                conn.rollback()
            due[name] = t0 + every
        if args.once:
            return
        time.sleep(max(1.0, min(due.values()) - time.time()))


if __name__ == "__main__":
    main()
