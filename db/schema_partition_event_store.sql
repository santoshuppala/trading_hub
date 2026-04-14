/**
 * Event Store Partitioning — Monthly Range Partitions
 *
 * SAFETY: This does NOT alter the existing event_store table.
 * It creates a NEW partitioned table and a migration function.
 * Run the migration AFTER market hours (post 4 PM PST).
 *
 * Benefits:
 * - Queries on recent data only scan relevant partition (~10x faster)
 * - Old partitions can be detached and archived
 * - VACUUM only processes active partitions
 * - Index maintenance is per-partition (smaller, faster)
 */

-- Step 1: Create partitioned table structure (identical columns)
CREATE TABLE IF NOT EXISTS event_store_partitioned (
    event_id              UUID NOT NULL,
    event_sequence        BIGINT NOT NULL,
    event_type            TEXT NOT NULL,
    event_version         INT NOT NULL DEFAULT 1,
    event_time            TIMESTAMP NOT NULL,
    received_time         TIMESTAMP NOT NULL,
    queued_time           TIMESTAMP,
    processed_time        TIMESTAMP NOT NULL,
    persisted_time        TIMESTAMP NOT NULL DEFAULT NOW(),
    aggregate_id          TEXT NOT NULL,
    aggregate_type        TEXT NOT NULL,
    aggregate_version     INT NOT NULL,
    event_payload         JSONB NOT NULL,
    correlation_id        UUID,
    causation_id          UUID,
    parent_event_id       UUID,
    source_system         TEXT NOT NULL,
    source_version        TEXT,
    session_id            UUID,
    PRIMARY KEY (event_id, event_time)  -- event_time in PK for partition routing
) PARTITION BY RANGE (event_time);

-- Step 2: Create monthly partitions (6 months ahead + 3 months back)
DO $$
DECLARE
    m_start DATE;
    m_end DATE;
    part_name TEXT;
BEGIN
    FOR i IN -3..6 LOOP
        m_start := date_trunc('month', CURRENT_DATE) + (i || ' months')::INTERVAL;
        m_end := m_start + '1 month'::INTERVAL;
        part_name := 'event_store_p_' || to_char(m_start, 'YYYY_MM');

        EXECUTE format(
            'CREATE TABLE IF NOT EXISTS %I PARTITION OF event_store_partitioned
             FOR VALUES FROM (%L) TO (%L)',
            part_name, m_start, m_end
        );
    END LOOP;
END $$;

-- Step 3: Default partition for out-of-range dates
CREATE TABLE IF NOT EXISTS event_store_p_default
    PARTITION OF event_store_partitioned DEFAULT;

-- Step 4: Indexes on partitioned table (created per partition automatically)
CREATE INDEX IF NOT EXISTS idx_esp_event_time ON event_store_partitioned (event_time DESC);
CREATE INDEX IF NOT EXISTS idx_esp_type ON event_store_partitioned (event_type, event_time DESC);
CREATE INDEX IF NOT EXISTS idx_esp_aggregate ON event_store_partitioned (aggregate_type, aggregate_id, event_sequence DESC);
CREATE INDEX IF NOT EXISTS idx_esp_correlation ON event_store_partitioned (correlation_id) WHERE correlation_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_esp_session ON event_store_partitioned (session_id, event_time DESC);

-- Step 5: Migration function (run AFTER hours)
CREATE OR REPLACE FUNCTION migrate_event_store_to_partitioned()
RETURNS BIGINT AS $$
DECLARE
    rows_migrated BIGINT := 0;
    batch_size INT := 10000;
    last_id UUID;
BEGIN
    -- Migrate in batches to avoid long locks
    LOOP
        WITH batch AS (
            SELECT * FROM event_store
            WHERE event_id NOT IN (SELECT event_id FROM event_store_partitioned)
            ORDER BY event_time
            LIMIT batch_size
        )
        INSERT INTO event_store_partitioned
        SELECT * FROM batch
        ON CONFLICT DO NOTHING;

        GET DIAGNOSTICS rows_migrated = ROW_COUNT;

        IF rows_migrated < batch_size THEN
            EXIT;
        END IF;

        -- Brief pause between batches to avoid blocking
        PERFORM pg_sleep(0.1);
    END LOOP;

    RETURN rows_migrated;
END;
$$ LANGUAGE plpgsql;

-- Step 6: Auto-create future partitions (run monthly via cron)
CREATE OR REPLACE FUNCTION create_next_partition()
RETURNS VOID AS $$
DECLARE
    next_month DATE;
    part_name TEXT;
BEGIN
    next_month := date_trunc('month', CURRENT_DATE + '2 months'::INTERVAL);
    part_name := 'event_store_p_' || to_char(next_month, 'YYYY_MM');

    EXECUTE format(
        'CREATE TABLE IF NOT EXISTS %I PARTITION OF event_store_partitioned
         FOR VALUES FROM (%L) TO (%L)',
        part_name, next_month, next_month + '1 month'::INTERVAL
    );

    RAISE NOTICE 'Created partition: %', part_name;
END;
$$ LANGUAGE plpgsql;

-- Grant permissions
GRANT SELECT, INSERT ON event_store_partitioned TO trading;
GRANT EXECUTE ON FUNCTION migrate_event_store_to_partitioned TO trading;
GRANT EXECUTE ON FUNCTION create_next_partition TO trading;
