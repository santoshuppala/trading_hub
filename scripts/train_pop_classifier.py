"""
Offline training script for Pop strategy ML classifier.

Usage:  python scripts/train_pop_classifier.py
Requires: xgboost, scikit-learn, psycopg2, joblib
Output:  data/pop_classifier.joblib

Reads historical PopStrategySignal + PositionClosed event pairs from the
event_store, extracts features, and trains an XGBoost multi-class classifier.
Uses TimeSeriesSplit to avoid look-ahead bias.
"""
import os
import sys
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(name)s %(message)s')
log = logging.getLogger(__name__)

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# Feature names must match MLStrategyClassifier._FEATURE_ATTRS
FEATURE_KEYS = [
    'atr_value', 'volatility_score', 'rvol', 'price_momentum', 'gap_pct',
    'vwap_distance', 'trend_cleanliness_score',
    'sentiment_score', 'sentiment_delta', 'headline_velocity',
    'social_velocity', 'social_sentiment_skew',
    'price',
]

STRATEGY_MAP = {
    'VWAP_RECLAIM': 0,
    'ORB': 1,
    'HALT_RESUME_BREAKOUT': 2,
    'EMA_TREND_CONTINUATION': 3,
    'BREAKOUT_PULLBACK': 4,
    'PARABOLIC_REVERSAL': 5,
}

NUM_CLASSES = len(STRATEGY_MAP)


def main():
    import json
    import numpy as np

    try:
        import psycopg2
        from config import DATABASE_URL
    except ImportError:
        log.error("psycopg2 required.  Install: pip install psycopg2-binary")
        return

    log.info("Connecting to database...")
    try:
        conn = psycopg2.connect(DATABASE_URL)
    except Exception as exc:
        log.error("DB connection failed: %s", exc)
        return

    # Query historical pop signals paired with their position-close outcomes
    query = """
    SELECT
        s.event_payload  AS signal_payload,
        c.event_payload  AS close_payload
    FROM event_store s
    JOIN event_store c ON c.correlation_id = s.event_id
    WHERE s.event_type = 'PopStrategySignal'
      AND c.event_type = 'PositionClosed'
    ORDER BY s.event_time
    """

    log.info("Fetching training data from event_store...")
    try:
        import pandas as pd
        df = pd.read_sql(query, conn)
        conn.close()
    except Exception as exc:
        log.error("Query failed: %s", exc)
        log.info(
            "If no PopStrategySignal events exist yet, run paper trading "
            "first to generate data."
        )
        conn.close()
        return

    if len(df) < 50:
        log.warning(
            "Only %d samples found. Need at least 50 for meaningful training.",
            len(df),
        )
        log.info(
            "Run the system in paper mode for a few sessions to accumulate "
            "training data."
        )
        return

    log.info("Found %d training samples", len(df))

    # ── Extract features and labels ──────────────────────────────────────────
    features = []
    labels = []

    for _, row in df.iterrows():
        try:
            sig = (
                json.loads(row['signal_payload'])
                if isinstance(row['signal_payload'], str)
                else row['signal_payload']
            )
            close = (
                json.loads(row['close_payload'])
                if isinstance(row['close_payload'], str)
                else row['close_payload']
            )

            feat = [float(sig.get(k, 0) or 0) for k in FEATURE_KEYS]
            features.append(feat)

            strategy = sig.get('strategy_type', 'VWAP_RECLAIM')
            label = STRATEGY_MAP.get(strategy, 0)
            labels.append(label)

        except Exception:
            continue

    if len(features) < 50:
        log.warning(
            "Only %d valid samples after parsing. Need more data.", len(features)
        )
        return

    X = np.array(features)
    y = np.array(labels)

    log.info("Training XGBoost classifier on %d samples...", len(X))

    try:
        import xgboost as xgb
        from sklearn.model_selection import TimeSeriesSplit
        from sklearn.metrics import classification_report
        import joblib
    except ImportError:
        log.error("Required: pip install xgboost scikit-learn joblib")
        return

    # ── Time-series cross-validation (no look-ahead bias) ────────────────────
    tscv = TimeSeriesSplit(n_splits=3)

    model = xgb.XGBClassifier(
        max_depth=6,
        n_estimators=100,
        learning_rate=0.1,
        objective='multi:softprob',
        num_class=NUM_CLASSES,
        eval_metric='mlogloss',
        use_label_encoder=False,
        verbosity=0,
    )

    for fold, (train_idx, val_idx) in enumerate(tscv.split(X)):
        X_train, X_val = X[train_idx], X[val_idx]
        y_train, y_val = y[train_idx], y[val_idx]
        model.fit(X_train, y_train)
        y_pred = model.predict(X_val)
        accuracy = (y_pred == y_val).mean()
        log.info("Fold %d accuracy: %.3f", fold + 1, accuracy)

    # Final train on all data
    model.fit(X, y)

    # ── Save model ───────────────────────────────────────────────────────────
    output_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data')
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, 'pop_classifier.joblib')
    joblib.dump(model, output_path)
    log.info("Model saved to %s", output_path)

    # ── Feature importance report ────────────────────────────────────────────
    importances = model.feature_importances_
    log.info("Feature importance:")
    for name, imp in sorted(
        zip(FEATURE_KEYS, importances), key=lambda x: -x[1]
    ):
        log.info("  %-25s %.4f", name, imp)


if __name__ == '__main__':
    main()
