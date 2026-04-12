-- ============================================================
-- 001_init.sql  — Bootstrap TimescaleDB extension
-- ============================================================

CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;

-- Idempotent schema
CREATE SCHEMA IF NOT EXISTS trading;

-- Enum types (used by multiple tables)
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'signal_action') THEN
        CREATE TYPE trading.signal_action AS ENUM (
            'BUY','SELL_STOP','SELL_TARGET','SELL_RSI','SELL_VWAP','PARTIAL_SELL','HOLD'
        );
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'order_side') THEN
        CREATE TYPE trading.order_side AS ENUM ('BUY','SELL');
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'position_action') THEN
        CREATE TYPE trading.position_action AS ENUM ('OPENED','PARTIAL_EXIT','CLOSED');
    END IF;
END
$$;
