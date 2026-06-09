-- ─── Market data schema ──────────────────────────────────────────────────────
-- Canonical reference for the market data tables.
-- The live schema is managed via Alembic migrations (infra/db/migrations/).
-- This file is the human-readable source of truth; always keep them in sync.
--
-- Unit conventions (see CLAUDE.md):
--   Prices  : NUMERIC(18,6), USD
--   Returns : NUMERIC(12,8), decimal (0.05 = 5%)
--   Volume  : BIGINT, shares
--   Timestamps : TIMESTAMPTZ, UTC

-- ─── Daily price bars ────────────────────────────────────────────────────────
-- Stores unadjusted OHLCV bars. Adjusted prices are derived at query time
-- by applying the cumulative corporate action adjustment factor.
-- Storing unadjusted prices is intentional: it preserves the original
-- data and makes adjustments fully auditable.

CREATE TABLE IF NOT EXISTS daily_prices (
    ticker          VARCHAR(20)     NOT NULL,
    date            DATE            NOT NULL,

    -- Unadjusted prices (raw from source)
    open            NUMERIC(18, 6),
    high            NUMERIC(18, 6),
    low             NUMERIC(18, 6),
    close           NUMERIC(18, 6)  NOT NULL,
    volume          BIGINT,

    -- Pre-computed adjusted close from source (yfinance / Polygon).
    -- Kept for cross-validation but not used in signal computation;
    -- we recompute adjustments ourselves from corporate_actions.
    source_adj_close NUMERIC(18, 6),

    -- Provenance
    source          VARCHAR(50)     NOT NULL DEFAULT 'yfinance',
    ingested_at     TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

    PRIMARY KEY (ticker, date)
);

-- Convert to TimescaleDB hypertable, partitioned by date.
-- chunk_time_interval = 1 month balances compression and query performance.
SELECT create_hypertable(
    'daily_prices', 'date',
    chunk_time_interval => INTERVAL '1 month',
    if_not_exists => TRUE
);

-- Supporting indexes
CREATE INDEX IF NOT EXISTS ix_daily_prices_ticker_date
    ON daily_prices (ticker, date DESC);

-- ─── Corporate actions ───────────────────────────────────────────────────────
-- Stores splits, cash dividends, and spinoffs with the ex-date.
-- The 'value' semantics depend on action_type:
--   split    : new_shares / old_shares  (e.g., 2.0 for a 2-for-1 split)
--   dividend : cash amount per share in USD
--   spinoff  : ratio of new entity shares per old entity share

CREATE TABLE IF NOT EXISTS corporate_actions (
    id              BIGSERIAL       PRIMARY KEY,
    ticker          VARCHAR(20)     NOT NULL,
    ex_date         DATE            NOT NULL,
    action_type     VARCHAR(20)     NOT NULL
                        CHECK (action_type IN ('split', 'dividend', 'spinoff')),
    value           NUMERIC(18, 6)  NOT NULL,
    notes           TEXT,
    source          VARCHAR(50)     NOT NULL DEFAULT 'yfinance',
    ingested_at     TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

    UNIQUE (ticker, ex_date, action_type)
);

CREATE INDEX IF NOT EXISTS ix_corporate_actions_ticker_exdate
    ON corporate_actions (ticker, ex_date DESC);

-- ─── Data ingestion log ──────────────────────────────────────────────────────
-- Tracks every ingestion batch for idempotency and debugging.
-- A batch that failed can be reprocessed by re-running with the same params;
-- the writer uses upserts so re-running is safe.

CREATE TABLE IF NOT EXISTS data_ingestion_log (
    id              BIGSERIAL       PRIMARY KEY,
    batch_id        UUID            NOT NULL DEFAULT gen_random_uuid(),
    source          VARCHAR(50)     NOT NULL,
    data_type       VARCHAR(50)     NOT NULL,  -- 'ohlcv', 'corporate_actions', etc.
    ticker          VARCHAR(20),               -- NULL means full-universe batch
    start_date      DATE,
    end_date        DATE,
    records_written INTEGER,
    status          VARCHAR(20)     NOT NULL
                        CHECK (status IN ('pending', 'complete', 'failed')),
    error_message   TEXT,
    raw_storage_path TEXT,                     -- MinIO path to raw API response
    started_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    completed_at    TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS ix_ingestion_log_started
    ON data_ingestion_log (started_at DESC);

-- ─── Data quality flags ──────────────────────────────────────────────────────
-- Anomalies detected during ingestion. Flagged records are still written
-- to daily_prices — exclusion is the caller's responsibility.
-- Unresolved flags with severity='error' block signal computation until reviewed.

CREATE TABLE IF NOT EXISTS data_quality_flags (
    id              BIGSERIAL       PRIMARY KEY,
    ticker          VARCHAR(20)     NOT NULL,
    date            DATE            NOT NULL,
    flag_type       VARCHAR(50)     NOT NULL,
        -- 'price_jump'       : close moved >3σ vs. recent history
        -- 'missing_data'     : expected bar not present
        -- 'volume_zero'      : volume reported as 0 on a trading day
        -- 'negative_price'   : price ≤ 0
        -- 'hloc_violation'   : high < low, or close outside [low, high]
    severity        VARCHAR(20)     NOT NULL
                        CHECK (severity IN ('warning', 'error')),
    message         TEXT            NOT NULL,
    resolved        BOOLEAN         NOT NULL DEFAULT FALSE,
    resolved_by     VARCHAR(100),
    resolved_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_quality_flags_ticker_date
    ON data_quality_flags (ticker, date DESC);
CREATE INDEX IF NOT EXISTS ix_quality_flags_unresolved
    ON data_quality_flags (resolved, severity)
    WHERE resolved = FALSE;
