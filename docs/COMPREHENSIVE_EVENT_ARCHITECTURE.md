# Comprehensive Event-Driven Architecture for Trading Hub

## Part 1: Current State Analysis

### Current Architecture Gaps

**What we have now**:
- Event sourcing for trading events (positions, fills, signals)
- TimescaleDB for time-series data
- Basic event correlation via `correlation_id`

**What we're missing**:
- ❌ System lifecycle events (restarts, crashes, recovery)
- ❌ Operational events (health checks, performance metrics)
- ❌ Risk events (blocks, check outcomes)
- ❌ Data quality events (mismatches, gaps, reconciliation)
- ❌ Error events (handler failures, timeouts, circuit breakers)
- ❌ Strategy performance events (metrics, backtests)
- ❌ Reconciliation events (Alpaca sync, position mismatches)
- ❌ Hierarchical event relationships
- ❌ Event causality chains (what caused what?)
- ❌ Real-time operational dashboards
- ❌ Historical trend analysis
- ❌ Anomaly detection capability

### Problem Statement

Currently, you can answer:
✅ "What trades happened?"
✅ "What was the P&L?"

But you **CANNOT** answer:
❌ "Why did we miss this trade?"
❌ "When did the system become unhealthy?"
❌ "What was the root cause of this anomaly?"
❌ "How did performance change over time?"
❌ "What were all the events leading to this crash?"
❌ "How many times did position reconciliation fail?"
❌ "Which strategies are degrading?"

---

## Part 2: Comprehensive Event Taxonomy

### Event Categories & Hierarchy

```
ALL_EVENTS
├── TRADING_EVENTS
│   ├── SignalGenerated (PRO, POP, OPTIONS)
│   ├── OrderRequested
│   ├── OrderRejected
│   ├── FillExecuted
│   ├── PositionOpened
│   ├── PositionPartialExit
│   ├── PositionClosed
│   ├── TradeCompleted
│   └── TradeMetrics
│
├── SYSTEM_EVENTS
│   ├── MonitorStarted
│   ├── MonitorStopped
│   ├── MonitorCrashed
│   ├── CrashRecovery
│   ├── SessionCreated
│   ├── SessionEnded
│   ├── ConfigurationLoaded
│   └── FeatureToggled
│
├── MARKET_EVENTS
│   ├── BarReceived
│   ├── BarProcessed
│   ├── QuoteReceived
│   ├── MarketOpen
│   ├── MarketClose
│   ├── RegularHoursEnd
│   └── PreMarketStart
│
├── RISK_EVENTS
│   ├── RiskCheckPassed
│   ├── RiskCheckFailed
│   ├── MaxPositionsReached
│   ├── MaxLossThresholdReached
│   ├── UnusualVolumeDetected
│   ├── PriceGapDetected
│   └── VolumeAlertTriggered
│
├── DATA_QUALITY_EVENTS
│   ├── DataGapDetected
│   ├── TimestampAnomaly
│   ├── MissingBarsDetected
│   ├── DuplicateEventDetected
│   ├── LatencyExceeded
│   └── DataConsistencyCheck
│
├── RECONCILIATION_EVENTS
│   ├── PositionReconcilationStarted
│   ├── PositionReconcilationCompleted
│   ├── PositionMismatch
│   ├── OrphanedPositionFound
│   ├── PositionSyncFailed
│   └── StateRecoveryCompleted
│
├── MONITORING_EVENTS
│   ├── HealthCheckPassed
│   ├── HealthCheckFailed
│   ├── ResourceExhaustion
│   ├── SlowHandlerDetected
│   ├── CircuitBreakerOpened
│   ├── CircuitBreakerClosed
│   ├── DatabaseLatencyHigh
│   └── QueueBackupDetected
│
├── ERROR_EVENTS
│   ├── HandlerFailed
│   ├── ExceptionThrown
│   ├── TimeoutOccurred
│   ├── ConnectionLost
│   ├── AuthenticationFailed
│   ├── ValidationFailed
│   └── RetryAttempt
│
├── STRATEGY_EVENTS
│   ├── StrategyEnabled
│   ├── StrategyDisabled
│   ├── StrategyMetricsComputed
│   ├── StrategyAnomaly
│   ├── StrategyBacktestCompleted
│   └── StrategyConfigChanged
│
├── BROKER_EVENTS
│   ├── BrokerConnected
│   ├── BrokerDisconnected
│   ├── OrderPlaced
│   ├── OrderCancelled
│   ├── OrderModified
│   ├── OrderFilled
│   ├── PortfolioUpdated
│   └── BrokerDataReceived
│
└── NOTIFICATION_EVENTS
    ├── AlertSent
    ├── AlertFailed
    ├── SlackNotified
    ├── EmailSent
    ├── WebhookTriggered
    └── NotificationDelivered
```

### Event Details

Each event category has specific event types with:

**Trading Events**:
- Signal source (strategy, tier, confidence)
- Entry/exit prices and logic
- Fill details and execution
- P&L calculation
- Trade lifecycle

**System Events**:
- Startup/shutdown sequence
- Session management
- Configuration changes
- Feature flags
- Version information

**Risk Events**:
- Check type and reason
- Pass/fail outcome
- Thresholds and values
- Risk metrics
- Mitigating actions

**Reconciliation Events**:
- Source comparison (local vs broker)
- Discrepancies found
- Resolution strategy
- Sync result
- Verification status

---

## Part 3: Advanced Event Store Design

### Extended event_store Schema

```sql
CREATE TABLE event_store (
    -- Event Identity
    event_id              UUID PRIMARY KEY,
    event_sequence        BIGINT,                    -- Global ordering
    event_type            TEXT NOT NULL,             -- 'PositionOpened', 'HealthCheckPassed'
    event_category        TEXT NOT NULL,             -- 'TRADING', 'SYSTEM', 'MONITORING'
    event_version         INT DEFAULT 1,             -- Schema versioning

    -- Complete Timestamp Trail
    event_time            TIMESTAMP NOT NULL,        -- When it happened
    received_time         TIMESTAMP NOT NULL,        -- When system got it
    processed_time        TIMESTAMP NOT NULL,        -- When we processed
    persisted_time        TIMESTAMP NOT NULL,        -- When written to DB

    -- Aggregate (Entity Being Changed)
    aggregate_id          TEXT NOT NULL,             -- 'position_ARM', 'session_xyz'
    aggregate_type        TEXT NOT NULL,             -- 'Position', 'Session', 'Risk'
    aggregate_version     INT NOT NULL,              -- Entity version

    -- Event Context & Causality
    correlation_id        UUID,                      -- Trace a logical flow
    causation_chain       UUID[],                    -- What caused this? [event1, event2, event3]
    parent_event_id       UUID,                      -- Direct parent
    child_event_ids       UUID[],                    -- Direct children
    session_id            UUID,                      -- Which monitor session?

    -- Event Payload
    event_payload         JSONB NOT NULL,            -- Complete data (flexible schema)
    tags                  TEXT[],                    -- 'critical', 'anomaly', 'block', 'slow'
    severity              TEXT,                      -- 'info', 'warning', 'error', 'critical'

    -- Computed Properties
    ingest_latency_ms     INT GENERATED,             -- received - event
    queue_latency_ms      INT GENERATED,             -- processed - received
    persistence_latency_ms INT GENERATED,            -- persisted - processed
    total_latency_ms      INT GENERATED,             -- persisted - event

    -- System Context
    source_system         TEXT NOT NULL,             -- 'PositionManager', 'RiskEngine'
    source_version        TEXT,                      -- 'v5.1'
    host_id               TEXT,                      -- Which machine?
    process_id            INT,                       -- Which process?

    -- Audit & Governance
    is_replayed           BOOLEAN DEFAULT FALSE,     -- Replayed from backup?
    is_anomaly            BOOLEAN DEFAULT FALSE,     -- Marked as anomalous?
    is_critical           BOOLEAN DEFAULT FALSE,     -- Requires attention?
    reviewed_by           TEXT,                      -- User who reviewed it
    review_notes          TEXT,                      -- Notes on review

    INDEX idx_event_category (event_category, event_time DESC),
    INDEX idx_aggregate (aggregate_type, aggregate_id, event_time DESC),
    INDEX idx_correlation (correlation_id),
    INDEX idx_causation (causation_chain),
    INDEX idx_tags (tags),
    INDEX idx_severity (severity, event_time DESC),
    INDEX idx_latency (total_latency_ms DESC),
    INDEX idx_anomaly (is_anomaly, event_time DESC)
);
```

### Event Payload Structure

```jsonc
{
  // Universal fields (all events have these)
  "event_id": "uuid",
  "timestamp": "2026-04-13T10:50:35.000Z",
  "sequence": 12345,

  // Category-specific fields (vary by type)

  // TRADING event example:
  {
    "ticker": "ARM",
    "action": "OPENED",
    "quantity": 5,
    "entry_price": 154.65,
    "strategy": "fib_confluence",
    "tier": 3,
    "signal_id": "uuid",
    "order_id": "order123",
    "fill_price": 154.66,
    "execution_venue": "alpaca",
    "slippage": 0.01,
    "confidence": 0.95
  }

  // RISK event example:
  {
    "check_type": "max_positions",
    "passed": false,
    "reason": "already_at_max_3_positions",
    "current_positions": 3,
    "max_allowed": 3,
    "attempted_ticker": "NVDA",
    "block_duration_seconds": 300,
    "next_check_time": "2026-04-13T10:55:35Z"
  }

  // MONITORING event example:
  {
    "check_type": "latency",
    "component": "DBWriter",
    "latency_ms": 2500,
    "threshold_ms": 1000,
    "status": "CRITICAL",
    "queue_size": 4950,
    "queue_max": 5000,
    "batch_size": 200,
    "batches_dropped": 5,
    "recommendation": "investigate_database_connection"
  }

  // RECONCILIATION event example:
  {
    "reconciliation_type": "position_sync",
    "source_local": ["AAPL", "NVDA", "TSLA"],
    "source_broker": ["AAPL", "NVDA", "TSLA", "ARM"],
    "orphaned_positions": ["ARM"],
    "missing_positions": [],
    "mismatch_details": {
      "ARM": {"local_qty": null, "broker_qty": 5, "action": "import"}
    },
    "reconciliation_status": "PARTIAL_SUCCESS",
    "positions_synced": 1,
    "positions_failed": 0
  }
}
```

---

## Part 4: Projection Architecture

### Core Projections (Rebuilt from Events)

#### 1. **realtime_state** (Current System State)
```sql
CREATE TABLE realtime_state (
    timestamp           TIMESTAMP PRIMARY KEY,

    -- Trading State
    open_positions      INT,
    total_trades        INT,
    session_pnl         DECIMAL(12, 2),
    daily_max_loss      DECIMAL(12, 2),

    -- System Health
    last_event_id       UUID,
    handler_status      JSONB,  -- {component: status}
    circuit_breaker_status JSONB,
    queue_depths        JSONB,  -- {DBWriter: 1234, Risk: 567}

    -- Risk State
    positions_blocked   INT,
    risk_checks_failed  INT,
    alerts_sent         INT,

    -- Data Quality
    latency_p95_ms      INT,
    data_gaps_detected  INT,

    -- Updated every heartbeat
    CONSTRAINT realtime_state_unique_latest UNIQUE (timestamp)
        WHERE timestamp = (SELECT MAX(timestamp) FROM realtime_state)
);
```

#### 2. **operation_timeline** (Chronological Event Log)
```sql
CREATE TABLE operation_timeline (
    event_id              UUID PRIMARY KEY,
    event_time            TIMESTAMP NOT NULL,
    event_category        TEXT NOT NULL,
    event_type            TEXT NOT NULL,
    aggregate_type        TEXT,
    aggregate_id          TEXT,
    severity              TEXT,
    description           TEXT GENERATED,  -- Human-readable summary

    INDEX idx_timeline (event_time DESC),
    INDEX idx_severity (severity, event_time DESC)
);
```

#### 3. **causality_chain** (Event Cause-Effect)
```sql
CREATE TABLE causality_chain (
    root_cause_id      UUID,               -- Original triggering event
    consequence_id     UUID,               -- Event caused by root
    depth              INT,                -- How many steps away?
    path_length        INT,                -- Total events in chain
    root_category      TEXT,
    consequence_category TEXT,
    created_at         TIMESTAMP,

    PRIMARY KEY (root_cause_id, consequence_id),
    INDEX idx_depth (depth),
    INDEX idx_root (root_cause_id)
);
```

#### 4. **anomaly_detection** (Patterns & Outliers)
```sql
CREATE TABLE anomaly_detection (
    anomaly_id          UUID PRIMARY KEY,
    anomaly_type        TEXT,              -- 'latency_spike', 'position_mismatch', etc.
    severity            TEXT,
    event_id            UUID NOT NULL,
    detected_at         TIMESTAMP,

    -- Anomaly Details
    metric              TEXT,              -- What was anomalous?
    expected_value      DECIMAL(15, 4),
    actual_value        DECIMAL(15, 4),
    deviation_pct       DECIMAL(5, 2),     -- % deviation
    z_score             DECIMAL(5, 2),     -- Standard deviations from mean

    -- Context
    baseline_window     INTERVAL,          -- How far back did we look?
    similar_events      INT,               -- How many similar events?

    -- Resolution
    acknowledged        BOOLEAN DEFAULT FALSE,
    acknowledged_by     TEXT,
    root_cause          TEXT,
    resolution_status   TEXT,              -- 'OPEN', 'INVESTIGATING', 'RESOLVED'

    INDEX idx_type (anomaly_type, detected_at DESC),
    INDEX idx_unresolved (resolution_status, detected_at DESC)
);
```

#### 5. **performance_metrics** (Strategy & System Performance)
```sql
CREATE TABLE performance_metrics (
    metric_id           UUID PRIMARY KEY,
    time_bucket         TIMESTAMP,         -- 5-min, 1-hour, 1-day buckets
    metric_type         TEXT,              -- 'strategy', 'system', 'risk'

    -- Trading Metrics
    strategy_name       TEXT,
    tier                INT,
    signals_generated   INT,
    signals_executed    INT,
    execution_rate      DECIMAL(5, 2),     -- % that got executed

    -- P&L Metrics
    trades_opened       INT,
    trades_closed       INT,
    pnl_total           DECIMAL(12, 2),
    pnl_avg             DECIMAL(10, 2),
    win_rate            DECIMAL(5, 2),

    -- Risk Metrics
    times_blocked       INT,
    max_loss_triggered  INT,

    -- System Metrics
    avg_latency_ms      INT,
    p95_latency_ms      INT,
    p99_latency_ms      INT,
    handler_errors      INT,
    circuit_breaks      INT,

    -- Efficiency
    db_queue_peak       INT,
    memory_peak_mb      INT,
    cpu_usage_pct       DECIMAL(5, 2),

    INDEX idx_metric_time (metric_type, time_bucket DESC),
    INDEX idx_strategy (strategy_name, time_bucket DESC)
);
```

#### 6. **event_dependency_graph** (System Interdependencies)
```sql
CREATE TABLE event_dependency_graph (
    from_component      TEXT,              -- 'PositionManager', 'RiskEngine'
    to_component        TEXT,
    event_type          TEXT,
    dependency_type     TEXT,              -- 'TRIGGERS', 'BLOCKED_BY', 'UPDATES'

    event_count         INT,               -- How many times?
    last_occurrence     TIMESTAMP,
    avg_latency_ms      INT,               -- Latency between events
    success_rate        DECIMAL(5, 2),

    PRIMARY KEY (from_component, to_component, event_type),
    INDEX idx_depends_on (to_component, from_component)
);
```

---

## Part 5: Real-Time Analysis Capabilities

### 1. Real-Time Health Dashboard Queries

```sql
-- System Health Status
SELECT
  component,
  status,
  last_heartbeat,
  latency_ms,
  queue_size,
  error_count_1h
FROM system_health
WHERE last_heartbeat > NOW() - INTERVAL '5 minutes'
ORDER BY error_count_1h DESC;

-- Identify Bottlenecks
SELECT
  from_component,
  to_component,
  avg_latency_ms,
  event_count_recent,
  success_rate,
  CASE
    WHEN avg_latency_ms > 1000 THEN 'CRITICAL'
    WHEN avg_latency_ms > 500 THEN 'WARNING'
    ELSE 'OK'
  END as status
FROM event_dependency_graph
WHERE last_occurrence > NOW() - INTERVAL '1 hour'
ORDER BY avg_latency_ms DESC;

-- Detect Anomalies
SELECT
  anomaly_type,
  COUNT(*) as count,
  MIN(detected_at) as first_seen,
  MAX(detected_at) as last_seen,
  STRING_AGG(DISTINCT severity, ',') as severity_levels
FROM anomaly_detection
WHERE detected_at > NOW() - INTERVAL '24 hours'
  AND resolution_status != 'RESOLVED'
GROUP BY anomaly_type
ORDER BY count DESC;
```

### 2. Historical Analysis

```sql
-- Trade Performance Over Time (Hourly)
SELECT
  DATE_TRUNC('hour', event_time) as hour,
  COUNT(CASE WHEN event_type = 'TradeCompleted' THEN 1 END) as trades,
  COUNT(CASE WHEN event_type = 'PositionClosed' AND
             (event_payload->>'pnl')::DECIMAL > 0 THEN 1 END) as wins,
  SUM((event_payload->>'pnl')::DECIMAL) as total_pnl,
  AVG(total_latency_ms) as avg_latency_ms
FROM event_store
WHERE event_category = 'TRADING'
  AND event_time > NOW() - INTERVAL '7 days'
GROUP BY DATE_TRUNC('hour', event_time)
ORDER BY hour DESC;

-- Strategy Performance Degradation Detection
SELECT
  strategy_name,
  DATE_TRUNC('hour', time_bucket) as hour,
  win_rate as current_win_rate,
  LAG(win_rate) OVER (
    PARTITION BY strategy_name ORDER BY time_bucket
  ) as previous_win_rate,
  win_rate - LAG(win_rate) OVER (
    PARTITION BY strategy_name ORDER BY time_bucket
  ) as change_pct,
  CASE
    WHEN win_rate - LAG(win_rate) OVER (
      PARTITION BY strategy_name ORDER BY time_bucket
    ) < -10 THEN 'SIGNIFICANT_DEGRADATION'
    WHEN win_rate < 45 THEN 'POOR_PERFORMANCE'
    ELSE 'NORMAL'
  END as status
FROM performance_metrics
WHERE metric_type = 'strategy'
  AND time_bucket > NOW() - INTERVAL '24 hours'
ORDER BY strategy_name, hour DESC;

-- Risk Block Analysis (Why were positions blocked?)
SELECT
  (event_payload->>'check_type') as block_reason,
  COUNT(*) as block_count,
  COUNT(DISTINCT (event_payload->>'attempted_ticker')) as unique_tickers,
  STRING_AGG(DISTINCT (event_payload->>'attempted_ticker'), ', ') as tickers,
  AVG((event_payload->>'block_duration_seconds')::INT) as avg_block_duration_s
FROM event_store
WHERE event_category = 'RISK'
  AND event_type = 'RiskCheckFailed'
  AND event_time > NOW() - INTERVAL '7 days'
GROUP BY event_payload->>'check_type'
ORDER BY block_count DESC;
```

### 3. Causality Analysis

```sql
-- Find Root Causes of Failed Trades
WITH failed_trade AS (
  SELECT event_id, event_payload
  FROM event_store
  WHERE event_type = 'TradeCompleted'
    AND (event_payload->>'pnl')::DECIMAL < -10  -- Large loss
),
root_causes AS (
  SELECT
    ft.event_id as failed_trade_id,
    cc.root_cause_id,
    ee.event_type,
    ee.event_category,
    ee.event_payload,
    cc.depth
  FROM failed_trade ft
  JOIN causality_chain cc ON ft.event_id = cc.consequence_id
  JOIN event_store ee ON cc.root_cause_id = ee.event_id
  WHERE cc.depth <= 3  -- Only immediate causes
)
SELECT
  failed_trade_id,
  STRING_AGG(DISTINCT event_type, ' → ') as event_chain,
  COUNT(*) as events_in_chain
FROM root_causes
GROUP BY failed_trade_id
ORDER BY events_in_chain DESC;
```

---

## Part 6: Implementation Roadmap

### Phase 1: Foundation (Week 1)
**Goal**: Extended event_store with rich metadata

```sql
-- 1. Extend event_store table
ALTER TABLE event_store ADD COLUMN event_category TEXT;
ALTER TABLE event_store ADD COLUMN causation_chain UUID[];
ALTER TABLE event_store ADD COLUMN tags TEXT[];
ALTER TABLE event_store ADD COLUMN severity TEXT;

-- 2. Create event type taxonomy
CREATE TABLE event_type_registry (
    event_type          TEXT PRIMARY KEY,
    event_category      TEXT NOT NULL,
    description         TEXT,
    payload_schema      JSONB,  -- JSON schema for validation
    parent_event_types  TEXT[],
    is_critical         BOOLEAN,
    requires_correlation BOOLEAN
);

-- 3. Populate taxonomy
INSERT INTO event_type_registry VALUES
    ('PositionOpened', 'TRADING', '...', '...', ARRAY[]::TEXT[], FALSE, TRUE),
    ('RiskCheckFailed', 'RISK', '...', '...', ARRAY[]::TEXT[], TRUE, TRUE),
    ('HealthCheckFailed', 'MONITORING', '...', '...', ARRAY[]::TEXT[], TRUE, FALSE),
    ...
```

### Phase 2: Core Projections (Week 2)
**Goal**: Build realtime_state and operation_timeline

```python
# In projection_builder.py
async def rebuild_realtime_state():
    """Current system state (single row, constantly updated)"""

async def rebuild_operation_timeline():
    """Chronological event summary"""

async def rebuild_causality_chains():
    """Link events via causation"""
```

### Phase 3: Analytics (Week 3)
**Goal**: Anomaly detection and performance metrics

```python
async def detect_anomalies():
    """Find unusual patterns:
    - Latency spikes
    - Win rate drops
    - Position mismatches
    - Handler errors
    """

async def compute_performance_metrics():
    """5-min, 1-hour, 1-day buckets"""
```

### Phase 4: Dashboard (Week 4)
**Goal**: Real-time visualization

```python
# Streamlit dashboard with:
# - Real-time health status
# - Event timeline
# - Anomaly alerts
# - Performance trends
# - Causality visualization
```

---

## Part 7: Advanced Capabilities

### 1. Event Replay for Debugging

```python
async def replay_events(
    start_time: datetime,
    end_time: datetime,
    filters: dict = None  # {"aggregate_type": "Position", "ticker": "ARM"}
) -> List[Event]:
    """Replay historical events for debugging."""

    # Query event_store
    events = await get_events(start_time, end_time, filters)

    # Reconstruct state at start_time
    initial_state = await reconstruct_state(start_time - 1)

    # Replay events one by one
    for event in events:
        # Show event details
        # Show state changes
        # Allow inspection at each step
```

### 2. What-If Analysis

```python
async def what_if_scenario(
    base_event: Event,
    modification: dict,
    replayed_events: List[Event]
) -> dict:
    """Simulate "what if this event happened differently?"

    Examples:
    - What if entry price was 0.1% lower?
    - What if we had more capital?
    - What if risk check passed?
    """

    modified_event = {**base_event, **modification}
    result = replay_from(modified_event, replayed_events)
    return {
        "original_pnl": calculate_pnl(replayed_events),
        "modified_pnl": result["pnl"],
        "difference": result["pnl"] - original_pnl,
    }
```

### 3. Trend Detection

```python
async def detect_trends(
    metric: str,  # 'win_rate', 'latency', 'drawdown'
    window_hours: int = 24,
    sensitivity: float = 2.0  # Std deviations
) -> List[Trend]:
    """Detect uptrends, downtrends, reversals."""

    # Calculate rolling statistics
    # Detect inflection points
    # Alert if significant trend
```

### 4. Correlation Analysis

```python
async def correlate_events(
    event_type_a: str,
    event_type_b: str,
    window_minutes: int = 5
) -> dict:
    """How often do two event types occur together?

    Examples:
    - RiskCheckFailed → TradeExecutionDelayed?
    - DatabaseLatencyHigh → HandlerTimeout?
    - PositionReconciliationFailed → PositionMismatch?
    """

    return {
        "correlation": 0.87,
        "occurrences_together": 42,
        "avg_time_between": 2.3,  # seconds
        "significance": "HIGH"
    }
```

---

## Part 8: Data Governance & Retention

### Event Lifecycle

```
Event Created (event_time)
    ↓
Event Logged (ingested_at)
    ↓
Event Processed (processed_time)
    ↓
Event Persisted (persisted_time)
    ↓
Event Analyzed (indexed, projected, aggregated)
    ↓
Event Reviewed (anomaly detection, correlation)
    ↓
Event Archived (after 30 days)
    ↓
Event Purged (after 2 years)
```

### Retention Policy

```sql
-- Hot data (1 day): Full detail, all indexes
-- Warm data (30 days): Full detail, limited indexes
-- Cold data (1 year): Compressed, read-only
-- Archive: Moved to S3/backup

CREATE POLICY hot_data AS
ON event_store FOR SELECT
  WHERE event_time > NOW() - INTERVAL '1 day'
  THEN ALL;

CREATE POLICY warm_data AS
ON event_store FOR SELECT
  WHERE event_time BETWEEN NOW() - INTERVAL '30 days'
                      AND NOW() - INTERVAL '1 day'
  THEN READ ONLY;
```

---

## Part 9: Schema Evolution & Versioning

### Handling Event Schema Changes

```python
# Versioned event payloads:
{
    "event_version": 2,
    "payload_v2": {...},
    "payload_v1_migration": {
        "from_version": 1,
        "migration_date": "2026-04-15",
        "breaking_changes": ["field_removed"]
    }
}

# Serialization handlers:
class EventDeserializer:
    def deserialize(event: dict) -> dict:
        version = event.get("event_version", 1)

        if version == 1:
            return migrate_v1_to_v2(event)
        elif version == 2:
            return event
        else:
            raise UnknownEventVersion()
```

---

## Part 10: Complete Example: Trade Failure Investigation

### Scenario
A large trade lost $150. We want to find out why.

### Query Chain

```python
# Step 1: Find the failed trade
failed_trade = await db.query("""
  SELECT event_id, event_payload FROM event_store
  WHERE event_type = 'TradeCompleted'
    AND (event_payload->>'pnl')::DECIMAL < -100
    AND event_time > NOW() - INTERVAL '1 hour'
  ORDER BY event_time DESC LIMIT 1
""")

# Step 2: Find root causes (causality chain)
root_causes = await db.query("""
  SELECT * FROM causality_chain
  WHERE consequence_id = $1
  ORDER BY depth ASC
""", failed_trade.event_id)

# Step 3: Understand the context (what was happening?)
context = await db.query("""
  SELECT
    event_type,
    event_time,
    event_payload->>'reason' as reason,
    total_latency_ms
  FROM event_store
  WHERE event_time BETWEEN $1 AND $2
    AND aggregate_id LIKE '%ARM%'
  ORDER BY event_time ASC
""", failed_trade.event_time - timedelta(minutes=5),
     failed_trade.event_time)

# Step 4: Check system health at that time
health = await db.query("""
  SELECT * FROM anomaly_detection
  WHERE detected_at BETWEEN $1 AND $2
    AND resolution_status != 'RESOLVED'
  ORDER BY severity DESC
""", failed_trade.event_time - timedelta(minutes=10),
     failed_trade.event_time + timedelta(minutes=5))

# Step 5: Replay the events to understand what happened
replay = await replay_events(
    start_time=failed_trade.event_time - timedelta(minutes=30),
    end_time=failed_trade.event_time,
    filters={"ticker": "ARM"}
)

# Step 6: Generate report
report = {
    "failed_trade": failed_trade,
    "root_causes": root_causes,
    "context": context,
    "system_health_at_time": health,
    "event_replay": replay,
    "conclusion": "..."  # AI-generated summary
}
```

---

## Part 11: Benefits & Impact

### What You'll Gain

✅ **Complete Operational Visibility**
- See every event in the system
- Understand what led to what
- No more "where did this come from?" questions

✅ **Root Cause Analysis**
- Causality chains show exact cause-effect
- Replay events to debug issues
- What-if analysis for decisions

✅ **Performance Optimization**
- Identify bottlenecks (slow handlers, queues)
- Track performance trends
- Detect degradation early

✅ **Risk Awareness**
- Monitor all risk checks in detail
- Understand why positions were blocked
- Detect unusual patterns

✅ **Data Quality Assurance**
- Detect anomalies automatically
- Identify data gaps
- Verify consistency

✅ **Strategic Insights**
- Strategy performance trends
- Correlation between events
- Predictive alerts

---

## Summary

This comprehensive event-driven architecture provides:

1. **Complete Event Taxonomy** - Captures ALL operational events
2. **Rich Metadata** - Full context, causality, correlation
3. **Advanced Projections** - Real-time state, analysis, anomalies
4. **Historical Analysis** - Trends, patterns, degradation
5. **Root Cause Tools** - Replay, causality, what-if
6. **Operational Dashboards** - Real-time visibility
7. **Data Governance** - Retention, versioning, evolution

**Result**: From reactive troubleshooting to proactive optimization.

Ready to implement? Start with Part 1 (extended event_store), then build projections incrementally.
