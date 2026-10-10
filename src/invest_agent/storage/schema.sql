-- storage/schema.sql: every table the app keeps in its one SQLite file.
--
-- What this file does:
--   Declares the tables for the decision log, the caches, the user profile and the
--   free-plan quota counts (design 14.19). storage/db.py runs this file every time
--   it opens the database. "CREATE TABLE IF NOT EXISTS" makes that safe: existing
--   tables and their rows are left alone, missing ones are created.
--
-- Dates are stored as ISO 8601 text in UTC (for example 2026-10-04T13:05:00+00:00),
-- which sorts correctly as text and is readable when you open the file.

-- One row per request. The columns are the fields worth filtering or sorting on;
-- everything else in 14.19 (profile snapshot, every source record, verification
-- log, scorecard, model input and output, guardrail log, sizing steps, year-1
-- projections, call log) goes in payload_json as one JSON document.
CREATE TABLE IF NOT EXISTS decision_log (
    request_id          TEXT PRIMARY KEY,
    created_at          TEXT NOT NULL,
    ticker              TEXT,
    exchange            TEXT,
    listing_currency    TEXT,
    verdict             TEXT,
    confidence          TEXT,
    recommended_amount  REAL,
    amount_currency     TEXT,
    config_version      TEXT,
    disclaimer_version  TEXT,
    cost_usd            REAL,
    latency_s           REAL,
    report_path         TEXT,
    payload_json        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decision_log_created ON decision_log (created_at);
CREATE INDEX IF NOT EXISTS idx_decision_log_ticker  ON decision_log (ticker);

-- All caches share one table. "namespace" says which cache a row belongs to
-- (fundamentals, price, macro, segment, fx, peer, news); each row carries its own
-- expiry time, so every cache can have a different lifetime.
CREATE TABLE IF NOT EXISTS cache (
    namespace   TEXT NOT NULL,
    key         TEXT NOT NULL,
    value_json  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    PRIMARY KEY (namespace, key)
);
CREATE INDEX IF NOT EXISTS idx_cache_expires ON cache (expires_at);

-- The stored user profile, merged with each new message in intake (14.2).
-- Single-user for now, so user_id is normally "local".
CREATE TABLE IF NOT EXISTS profiles (
    user_id       TEXT PRIMARY KEY,
    profile_json  TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

-- Running free-plan usage per service. period_kind is "day" or "month" and
-- period_key is the day ("2026-10-04") or month ("2026-10") the count belongs to,
-- so a new day or month starts a fresh row automatically.
CREATE TABLE IF NOT EXISTS quota_usage (
    service      TEXT NOT NULL,
    period_kind  TEXT NOT NULL CHECK (period_kind IN ('day', 'month')),
    period_key   TEXT NOT NULL,
    units        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (service, period_kind, period_key)
);

-- Backtesting (step 15, design 14.17). One row per scored live decision: the stock's
-- 12-month return against its benchmark and whether the verdict held up.
CREATE TABLE IF NOT EXISTS verdict_scores (
    request_id      TEXT PRIMARY KEY,
    scored_at       TEXT NOT NULL,
    ticker          TEXT,
    verdict         TEXT,
    confidence      TEXT,
    excess_return   REAL,
    correct         INTEGER,
    result_json     TEXT NOT NULL
);

-- One row per scored year-1 projection: the projected revenue and profit against what
-- the next annual report showed, and whether the actual landed inside the range.
CREATE TABLE IF NOT EXISTS forecast_scores (
    request_id      TEXT PRIMARY KEY,
    scored_at       TEXT NOT NULL,
    ticker          TEXT,
    fiscal_year     INTEGER,
    revenue_error   REAL,
    revenue_inside  INTEGER,
    profit_error    REAL,
    profit_inside   INTEGER,
    result_json     TEXT NOT NULL
);
