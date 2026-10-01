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

-- weekly paper orders (one position per market; the arm trades only No)
CREATE TABLE IF NOT EXISTS weekly.orders (
    id            BIGSERIAL PRIMARY KEY,
    created_at    BIGINT NOT NULL,
    arm           TEXT NOT NULL,
    condition_id  TEXT NOT NULL,
    event_slug    TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    direction     TEXT NOT NULL,
    strike        DOUBLE PRECISION NOT NULL,
    side          TEXT NOT NULL,             -- yes (touch) | no (no touch)
    token_id      TEXT NOT NULL,
    end_ts        BIGINT NOT NULL,
    tau_h         DOUBLE PRECISION,
    spot          DOUBLE PRECISION,          -- proxy price, offset-corrected
    var_left      DOUBLE PRECISION,          -- k^2 * session variance to the end
    model_p       DOUBLE PRECISION,          -- P(this side wins)
    best_bid      DOUBLE PRECISION,
    best_ask      DOUBLE PRECISION,
    ask_size      DOUBLE PRECISION,
    edge          DOUBLE PRECISION,
    limit_price   DOUBLE PRECISION,
    shares_req    DOUBLE PRECISION,
    asks          JSONB,
    status        TEXT NOT NULL,             -- filled | partial | nofill
    shares_filled DOUBLE PRECISION NOT NULL DEFAULT 0,
    avg_price     DOUBLE PRECISION,
    fee           DOUBLE PRECISION NOT NULL DEFAULT 0,
    outcome       DOUBLE PRECISION,          -- payout per share of this side
    pnl           DOUBLE PRECISION,
    settled_at    BIGINT
);
CREATE UNIQUE INDEX IF NOT EXISTS weekly_orders_one_position
    ON weekly.orders (arm, condition_id) WHERE status IN ('filled', 'partial');

-- Yahoo 1h bars: years of history for the touch backtest (1m only covers ~8 days)
CREATE TABLE IF NOT EXISTS weekly.bars_1h (
    symbol TEXT NOT NULL,
    ts     BIGINT NOT NULL,                  -- bar open
    open DOUBLE PRECISION, high DOUBLE PRECISION, low DOUBLE PRECISION,
    close DOUBLE PRECISION, volume DOUBLE PRECISION,
    PRIMARY KEY (symbol, ts)
);
"""
