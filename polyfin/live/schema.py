"""Trading tables, in their own `trade` schema.  Written only by the trader."""

TRADE_SCHEMA = """
CREATE SCHEMA IF NOT EXISTS trade;

-- One row.  The ratchet: a process launched without --live can never trade live,
-- whatever this says; a process launched with --live trades live only while this
-- says 'live'.  'paused' stops new entries (settlement keeps running).
CREATE TABLE IF NOT EXISTS trade.control (
    id         INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    mode       TEXT NOT NULL DEFAULT 'paper' CHECK (mode IN ('paused', 'paper', 'live')),
    reason     TEXT,
    updated_at BIGINT
);
INSERT INTO trade.control (id, mode, reason, updated_at)
VALUES (1, 'paper', 'initial', EXTRACT(EPOCH FROM now())::BIGINT)
ON CONFLICT (id) DO NOTHING;

-- One row per entry attempt, paper and live alike.  The intent is written
-- (status 'pending') BEFORE the order is sent, then updated with the result.
CREATE TABLE IF NOT EXISTS trade.orders (
    id            BIGSERIAL PRIMARY KEY,
    created_at    BIGINT NOT NULL,
    mode          TEXT NOT NULL CHECK (mode IN ('paper', 'live')),
    condition_id  TEXT NOT NULL,
    series_slug   TEXT NOT NULL,
    kind          TEXT NOT NULL,
    strike        DOUBLE PRECISION,
    side          TEXT NOT NULL CHECK (side IN ('yes', 'no')),
    token_id      TEXT NOT NULL,
    target_ts     BIGINT NOT NULL,
    tau_h         DOUBLE PRECISION,          -- hours to target at decision
    model_p       DOUBLE PRECISION,          -- P(this side wins)
    best_bid      DOUBLE PRECISION,          -- of this side's token
    best_ask      DOUBLE PRECISION,
    ask_size      DOUBLE PRECISION,
    edge          DOUBLE PRECISION,          -- model_p - best_ask - fee per share
    limit_price   DOUBLE PRECISION,
    shares_req    DOUBLE PRECISION,
    asks          JSONB,                     -- ask ladder at decision, best first
    features      JSONB,                     -- model inputs, for later analysis
    status        TEXT NOT NULL,             -- pending|filled|partial|nofill|rejected|unknown|failed
    shares_filled DOUBLE PRECISION NOT NULL DEFAULT 0,
    avg_price     DOUBLE PRECISION,
    fee           DOUBLE PRECISION NOT NULL DEFAULT 0,
    venue_order_id TEXT,
    error         TEXT,
    filled_at     BIGINT,
    outcome       DOUBLE PRECISION,          -- payout per share of this side: 1, 0, 0.5
    pnl           DOUBLE PRECISION,          -- shares*(outcome-avg_price) - fee
    settled_at    BIGINT
);
CREATE INDEX IF NOT EXISTS orders_created ON trade.orders (created_at);
CREATE INDEX IF NOT EXISTS orders_unsettled ON trade.orders (condition_id)
    WHERE settled_at IS NULL AND shares_filled > 0;
-- live-only bookkeeping (added after the first paper rows existed)
ALTER TABLE trade.orders ADD COLUMN IF NOT EXISTS trade_ids     JSONB;
ALTER TABLE trade.orders ADD COLUMN IF NOT EXISTS settle_state  TEXT;    -- confirmed|failed|NULL
ALTER TABLE trade.orders ADD COLUMN IF NOT EXISTS reconciled_at BIGINT;  -- checked against venue
ALTER TABLE trade.orders ADD COLUMN IF NOT EXISTS redeemed_at   BIGINT;
ALTER TABLE trade.orders ADD COLUMN IF NOT EXISTS redeemed_by   TEXT;    -- auto|relayer|none
ALTER TABLE trade.orders ADD COLUMN IF NOT EXISTS redeem_detail TEXT;

-- at most one live-or-possibly-live position per market and mode
CREATE UNIQUE INDEX IF NOT EXISTS orders_one_position ON trade.orders (mode, condition_id)
    WHERE status IN ('pending', 'filled', 'partial', 'unknown');
"""
