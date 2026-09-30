"""One-off: copy the SQLite recording (data/polyfin.sqlite) into PostgreSQL.

    .venv/bin/python scripts/migrate_sqlite.py [path/to/polyfin.sqlite]

Idempotent: rows already present are skipped (ON CONFLICT DO NOTHING), so it
can be re-run after the recorder has switched to Postgres.
"""
import sqlite3
import sys
import time
from pathlib import Path

import psycopg2.extras

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from polyfin.db import connect  # noqa: E402

TABLES = {
    "bars": "symbol, ts, open, high, low, close, volume",
    "markets": "condition_id, event_slug, series_slug, kind, symbol, question, strike, "
               "token_yes, token_no, event_start, end_ts, closed, outcome_yes, "
               "resolution_source, history_ts, updated_at, raw",
    "pm_history": "token_id, ts, p",
    "pm_books": "token_id, ts, best_bid, bid_size, best_ask, ask_size, bids, asks",
}


def main() -> None:
    src = Path(sys.argv[1] if len(sys.argv) > 1 else "data/polyfin.sqlite")
    lite = sqlite3.connect(str(src))
    pg = connect()
    for table, cols in TABLES.items():
        t0 = time.time()
        cur = lite.execute(f"SELECT {cols} FROM {table}")
        n = 0
        with pg.conn.cursor() as pc:
            while True:
                rows = cur.fetchmany(20000)
                if not rows:
                    break
                psycopg2.extras.execute_values(
                    pc, f"INSERT INTO {table} ({cols}) VALUES %s ON CONFLICT DO NOTHING",
                    rows, page_size=5000)
                n += len(rows)
        pg.commit()
        total = pg.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"{table:10} read {n:8d}  now {total:8d} rows in postgres  ({time.time() - t0:.0f}s)")


if __name__ == "__main__":
    main()
