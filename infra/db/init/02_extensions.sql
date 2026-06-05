-- Enable extensions on the rqis database.
-- TimescaleDB extension must be created before any hypertable is defined.

\connect rqis

CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;
CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
