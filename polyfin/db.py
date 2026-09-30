"""PostgreSQL storage (TimescaleDB container, see docker-compose.yml).

All timestamps are unix seconds (UTC) in BIGINT columns.  Market data lives in
the `public` schema (written by the recorder only); trading state lives in the
`trade` schema (written by the trader only).

    python -m polyfin.db      # create / update the schema (idempotent)
"""
from __future__ import annotations

import psycopg2
import psycopg2.extras

from .settings import pg_dsn

SCHEMA = """
-- underlying 1-minute bars (Yahoo); ts = bar open
CREATE TABLE IF NOT EXISTS bars (
    symbol TEXT NOT NULL,
    ts     BIGINT NOT NULL,
    open DOUBLE PRECISION, high DOUBLE PRECISION, low DOUBLE PRECISION,
    close DOUBLE PRECISION, volume DOUBLE PRECISION,
    PRIMARY KEY (symbol, ts)
);

-- one row per Polymarket market (a strike event has one per strike)
CREATE TABLE IF NOT EXISTS markets (
    condition_id  TEXT PRIMARY KEY,
    event_slug    TEXT NOT NULL,
    series_slug   TEXT NOT NULL,
    kind          TEXT NOT NULL,          -- updown | open | strikes
    symbol        TEXT,                   -- Yahoo underlying, NULL if none
    question      TEXT,
    strike        DOUBLE PRECISION,       -- strikes only
    token_yes     TEXT,                   -- outcome 0 ("Up" / "Yes")
    token_no      TEXT,
    event_start   BIGINT,
    end_ts        BIGINT,                 -- settlement time
    closed        INTEGER NOT NULL DEFAULT 0,
    outcome_yes   DOUBLE PRECISION,       -- final price of outcome 0 once resolved (1, 0, 0.5)
    resolution_source TEXT,
    history_ts    BIGINT,                 -- prices-history recorded through this ts
    updated_at    BIGINT,
    raw           JSONB
);
CREATE INDEX IF NOT EXISTS markets_end ON markets (end_ts);
CREATE INDEX IF NOT EXISTS markets_token_yes ON markets (token_yes);

-- CLOB prices-history for token_yes (~1-minute points)
CREATE TABLE IF NOT EXISTS pm_history (
    token_id TEXT NOT NULL,
    ts       BIGINT NOT NULL,
    p        DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (token_id, ts)
);

-- order-book snapshots for token_yes (the No book is its mirror)
CREATE TABLE IF NOT EXISTS pm_books (
    token_id TEXT NOT NULL,
    ts       BIGINT NOT NULL,             -- CLOB book timestamp
    best_bid DOUBLE PRECISION, bid_size DOUBLE PRECISION,
    best_ask DOUBLE PRECISION, ask_size DOUBLE PRECISION,
    bids     JSONB,                       -- [[price, size], ...] best first
    asks     JSONB,
    PRIMARY KEY (token_id, ts)
);
"""


class DB:
    """A psycopg2 connection with a sqlite-like `execute` that returns the cursor."""

    def __init__(self, dsn: str | None = None):
        self.conn = psycopg2.connect(dsn or pg_dsn())

    def execute(self, sql: str, params=()):
        cur = self.conn.cursor()
        cur.execute(sql, params)
        return cur

    def executemany(self, sql: str, rows) -> None:
        rows = list(rows)
        if rows:
            with self.conn.cursor() as cur:
                psycopg2.extras.execute_batch(cur, sql, rows, page_size=1000)

    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()

    def close(self) -> None:
        self.conn.close()


def connect(dsn: str | None = None, migrate: bool = True) -> DB:
    db = DB(dsn)
    if migrate:
        db.execute(SCHEMA)
        from .live.schema import TRADE_SCHEMA   # trading tables share the database
        db.execute(TRADE_SCHEMA)
        db.commit()
    return db


if __name__ == "__main__":
    connect().close()
    print("schema up to date")
