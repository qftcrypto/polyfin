"""SQLite storage.  All timestamps are unix seconds (UTC)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "data" / "polyfin.sqlite"

SCHEMA = """
-- underlying 1-minute bars (Yahoo); ts = bar open
CREATE TABLE IF NOT EXISTS bars (
    symbol TEXT NOT NULL,
    ts     INTEGER NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume REAL,
    PRIMARY KEY (symbol, ts)
) WITHOUT ROWID;

-- one row per Polymarket market (a strike event has one per strike)
CREATE TABLE IF NOT EXISTS markets (
    condition_id  TEXT PRIMARY KEY,
    event_slug    TEXT NOT NULL,
    series_slug   TEXT NOT NULL,
    kind          TEXT NOT NULL,          -- updown | open | strikes
    symbol        TEXT,                   -- Yahoo underlying, NULL if none
    question      TEXT,
    strike        REAL,                   -- strikes only
    token_yes     TEXT,                   -- outcome 0 ("Up" / "Yes")
    token_no      TEXT,
    event_start   INTEGER,
    end_ts        INTEGER,                -- settlement time
    closed        INTEGER NOT NULL DEFAULT 0,
    outcome_yes   REAL,                   -- final price of outcome 0 once resolved (1, 0, 0.5)
    resolution_source TEXT,
    history_ts    INTEGER,                -- prices-history recorded through this ts
    updated_at    INTEGER,
    raw           TEXT
);
CREATE INDEX IF NOT EXISTS markets_end ON markets (end_ts);

-- CLOB prices-history for token_yes (~1-minute points)
CREATE TABLE IF NOT EXISTS pm_history (
    token_id TEXT NOT NULL,
    ts       INTEGER NOT NULL,
    p        REAL NOT NULL,
    PRIMARY KEY (token_id, ts)
) WITHOUT ROWID;

-- order-book snapshots for token_yes (the No book is its mirror)
CREATE TABLE IF NOT EXISTS pm_books (
    token_id TEXT NOT NULL,
    ts       INTEGER NOT NULL,            -- CLOB book timestamp
    best_bid REAL, bid_size REAL,
    best_ask REAL, ask_size REAL,
    bids     TEXT,                        -- JSON [[price, size], ...] best first
    asks     TEXT,
    PRIMARY KEY (token_id, ts)
) WITHOUT ROWID;
"""


def connect(path: str | Path = DEFAULT_PATH) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    return conn
