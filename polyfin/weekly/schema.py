"""Weekly tables, schema `weekly`.  Timestamps are unix seconds (UTC)."""

WEEKLY_SCHEMA = """
CREATE SCHEMA IF NOT EXISTS weekly;

-- one row per touch market (one strike, one direction)
CREATE TABLE IF NOT EXISTS weekly.markets (
    condition_id  TEXT PRIMARY KEY,
    event_slug    TEXT NOT NULL,
    series_slug   TEXT NOT NULL,
    symbol        TEXT NOT NULL,             -- Yahoo proxy
    session       TEXT NOT NULL,             -- rth | cme | fx
    direction     TEXT NOT NULL,             -- up (High >= strike) | down (Low <= strike)
    strike        DOUBLE PRECISION NOT NULL,
    token_yes     TEXT NOT NULL,
    token_no      TEXT NOT NULL,
    created_ts    BIGINT,                    -- touches count only after creation
    end_ts        BIGINT NOT NULL,
    closed        INTEGER NOT NULL DEFAULT 0,
    outcome_yes   DOUBLE PRECISION,          -- 1 touched, 0 not
    closed_ts     BIGINT,                    -- when the market closed (touch or expiry)
    volume        DOUBLE PRECISION,
    history_ts    BIGINT,                    -- pm_history recorded through this ts
    updated_at    BIGINT,
    raw           JSONB
);
CREATE INDEX IF NOT EXISTS weekly_markets_end ON weekly.markets (end_ts);
CREATE INDEX IF NOT EXISTS weekly_markets_event ON weekly.markets (event_slug);

-- Yahoo 1h bars: years of history for the touch backtest (1m only covers ~8 days)
CREATE TABLE IF NOT EXISTS weekly.bars_1h (
    symbol TEXT NOT NULL,
    ts     BIGINT NOT NULL,                  -- bar open
    open DOUBLE PRECISION, high DOUBLE PRECISION, low DOUBLE PRECISION,
    close DOUBLE PRECISION, volume DOUBLE PRECISION,
    PRIMARY KEY (symbol, ts)
);
"""
