"""
demo_pop_strategies.py — End-to-end demonstration of the pop-strategy subsystem
================================================================================
Runs the full pipeline with synthetic data:
  ingestion → features → screener → classifier → strategy_router

Then simulates EventBus POP_SIGNAL + SIGNAL emission and prints a structured
summary of every pop candidate, its assigned strategy, and generated signals.

Usage
-----
    cd trading_hub
    python demo_pop_strategies.py

    # Verbose mode (shows full feature vectors)
    python demo_pop_strategies.py --verbose

    # Specific symbols only
    python demo_pop_strategies.py --symbols AAPL NVDA TSLA

No API keys required — all data is synthetic / mocked.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import textwrap
from typing import List, Optional

# ── Logging setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.WARNING,
    format='%(levelname)s %(name)s: %(message)s',
)
log = logging.getLogger('demo_pop_strategies')


def main(symbols: Optional[List[str]] = None, verbose: bool = False) -> None:
    from pop_screener.ingestion    import (
        MarketBehaviorSource, MomentumSource,
        NewsSentimentSource, SocialSentimentSource,
    )
    from pop_screener.features     import FeatureEngineer
    from pop_screener.screener     import PopScreener
    from pop_screener.classifier   import StrategyClassifier
    from pop_screener.strategy_router import StrategyRouter
    from pop_screener.models       import (
        EntrySignal, ExitSignal, PopCandidate,
        StrategyAssignment, TradeCandidate,
    )

    # ── Source adapters (all mocked) ──────────────────────────────────────────
    news_src    = NewsSentimentSource()
    social_src  = SocialSentimentSource()
    market_src  = MarketBehaviorSource()
    mom_src     = MomentumSource(universe=symbols)

    # ── Pipeline components ───────────────────────────────────────────────────
    engineer   = FeatureEngineer()
    screener   = PopScreener()
    classifier = StrategyClassifier()
    router     = StrategyRouter()

    # ── Universe ──────────────────────────────────────────────────────────────
    universe = mom_src.get_momentum_universe()
    print_banner(f"Pop-Strategy Demo  —  universe: {len(universe)} symbols")

    # ── Run pipeline per symbol ───────────────────────────────────────────────
    trade_candidates: List[TradeCandidate] = []
    skipped = 0

    for symbol in universe:
        # 1. Ingestion
        news_1h  = news_src.get_news(symbol, window_hours=1.0)
        news_24h = news_src.get_news(symbol, window_hours=24.0)
        social   = social_src.get_social(symbol, window_hours=1.0)
        market   = market_src.get_market_slice(symbol, window_bars=60)

        # 2. Features
        features = engineer.compute(
            symbol=symbol,
            news_1h=news_1h,
            news_24h=news_24h,
            social=social,
            market=market,
            headline_baseline_velocity=2.0,
            social_baseline_velocity=100.0,
        )

        # 3. Screening
        candidate = screener.screen(features)
        if candidate is None:
            skipped += 1
            continue

        # 4. Classification
        assignment = classifier.classify(candidate)

        # 5. Signal generation
        entries, exits = router.route(
            symbol=symbol,
            bars=market.bars,
            vwap_series=market.vwap_series,
            features=features,
            assignment=assignment,
        )

        trade_candidates.append(TradeCandidate(
            pop_candidate=candidate,
            strategy_assignment=assignment,
            entry_signals=entries,
            exit_signals=exits,
        ))

    # ── Print results ─────────────────────────────────────────────────────────
    print(f"\n{'='*72}")
    print(f"  Screened {len(universe)} symbols → "
          f"{len(trade_candidates)} pop candidates  "
          f"({skipped} skipped)")
    print(f"{'='*72}\n")

    if not trade_candidates:
        print("No pop candidates found with current synthetic data.")
        print("(Try re-running — the mock data uses a fixed seed but some "
              "symbols may not pass all thresholds.)")
        return

    for tc in trade_candidates:
        _print_trade_candidate(tc, verbose=verbose)

    # ── Simulate EventBus emission ────────────────────────────────────────────
    print(f"\n{'='*72}")
    print("  EventBus simulation")
    print(f"{'='*72}\n")
    _simulate_eventbus(trade_candidates)

    # ── Summary table ──────────────────────────────────────────────────────────
    print(f"\n{'='*72}")
    print("  Summary")
    print(f"{'='*72}")
    _print_summary(trade_candidates)


# ── Printing helpers ───────────────────────────────────────────────────────────

def print_banner(title: str) -> None:
    width = 72
    print('=' * width)
    print(f"  {title}")
    print('=' * width)


def _print_trade_candidate(tc, verbose: bool = False) -> None:
    cand = tc.pop_candidate
    asgn = tc.strategy_assignment
    f    = cand.features

    print(f"┌─ {cand.symbol}  [{cand.pop_reason}]")
    print(f"│  Primary strategy  : {asgn.primary_strategy}")
    if asgn.secondary_strategies:
        print(f"│  Secondaries       : {[str(s) for s in asgn.secondary_strategies]}")
    print(f"│  Confidence        : {asgn.strategy_confidence:.0%}")
    print(f"│  VWAP compat       : {asgn.vwap_compatibility_score:.0%}")
    print(f"│  Note              : {textwrap.shorten(asgn.notes, 70)}")

    if verbose:
        print(f"│  ── Features ─────────────────────────────────────────────")
        print(f"│  last_price        : ${f.last_price:.2f}")
        print(f"│  RVOL              : {f.rvol:.2f}×")
        print(f"│  Gap size          : {f.gap_size:.2%}")
        print(f"│  Sentiment delta   : {f.sentiment_delta:+.3f}")
        print(f"│  Headline velocity : {f.headline_velocity:.2f}×")
        print(f"│  Social velocity   : {f.social_velocity:.2f}×")
        print(f"│  Social skew       : {f.social_sentiment_skew:+.3f}")
        print(f"│  Volatility score  : {f.volatility_score:.4f}")
        print(f"│  Price momentum    : {f.price_momentum:.4f}")
        print(f"│  VWAP distance     : {f.vwap_distance:+.4f}")
        print(f"│  Trend cleanliness : {f.trend_cleanliness_score:.3f}")
        print(f"│  ATR               : ${f.atr_value:.4f}")

    if tc.entry_signals:
        for sig in tc.entry_signals:
            _print_entry_signal(sig)
    else:
        print(f"│  ⚠  No entry signals fired this bar")

    if tc.exit_signals:
        for sig in tc.exit_signals:
            print(f"│  EXIT  {sig.reason:20s}  @ ${sig.exit_price:.4f}")

    print(f"└{'─' * 70}")
    print()


def _print_entry_signal(sig) -> None:
    side_icon = '↑ LONG' if sig.side == 'buy' else '↓ SHORT'
    r_to_t2 = (
        (sig.target_2 - sig.entry_price) / (sig.entry_price - sig.stop_loss)
        if sig.side == 'buy' and (sig.entry_price - sig.stop_loss) > 0
        else (sig.entry_price - sig.target_2) / (sig.stop_loss - sig.entry_price)
        if (sig.stop_loss - sig.entry_price) > 0 else 0.0
    )
    print(f"│  {side_icon:<8}  strategy={sig.strategy_type}")
    print(f"│      Entry    : ${sig.entry_price:.4f}")
    print(f"│      Stop     : ${sig.stop_loss:.4f}  "
          f"(-{abs(sig.entry_price - sig.stop_loss) / sig.entry_price:.2%})")
    print(f"│      Target 1 : ${sig.target_1:.4f}  "
          f"(+{abs(sig.target_1 - sig.entry_price) / sig.entry_price:.2%})")
    print(f"│      Target 2 : ${sig.target_2:.4f}  "
          f"(+{abs(sig.target_2 - sig.entry_price) / sig.entry_price:.2%}  "
          f"R={r_to_t2:.1f}R)")


def _simulate_eventbus(trade_candidates) -> None:
    """
    Simulate EventBus POP_SIGNAL and SIGNAL emission without actually
    starting the bus (which requires broker credentials and Redpanda).
    """
    from monitor.events import PopSignalPayload, SignalPayload, SignalAction

    pop_count    = 0
    signal_count = 0
    errors       = 0

    for tc in trade_candidates:
        features = tc.pop_candidate.features
        asgn     = tc.strategy_assignment

        for entry in tc.entry_signals:
            # ── POP_SIGNAL payload construction ───────────────────────────────
            try:
                atr = max(entry.metadata.get('atr', features.atr_value), 1e-6)
                snap = {'rvol': features.rvol, 'gap': features.gap_size}
                pop_payload = PopSignalPayload(
                    symbol=entry.symbol,
                    strategy_type=str(entry.strategy_type),
                    entry_price=entry.entry_price,
                    stop_price=entry.stop_loss,
                    target_1=entry.target_1,
                    target_2=entry.target_2,
                    pop_reason=str(tc.pop_candidate.pop_reason),
                    atr_value=atr,
                    rvol=features.rvol,
                    vwap_distance=features.vwap_distance,
                    strategy_confidence=asgn.strategy_confidence,
                    features_json=json.dumps(snap),
                )
                pop_count += 1
                print(f"  [POP_SIGNAL]  {pop_payload.symbol:8s} "
                      f"strategy={pop_payload.strategy_type:25s} "
                      f"confidence={pop_payload.strategy_confidence:.0%}  ✓")
            except Exception as exc:
                print(f"  [POP_SIGNAL]  {entry.symbol:8s}  ERROR: {exc}")
                errors += 1
                continue

            # ── SIGNAL(BUY) payload construction (long entries only) ──────────
            if entry.side != 'buy':
                print(f"  [SIGNAL]      {entry.symbol:8s}  (short — skipped)")
                continue

            try:
                vwap  = features.current_vwap if features.current_vwap > 0 else entry.entry_price
                rsi   = float(entry.metadata.get('rsi', 55.0))
                reclaim_low = float(entry.metadata.get('reclaim_candle_low', entry.stop_loss))

                signal_payload = SignalPayload(
                    ticker=entry.symbol,
                    action=SignalAction.BUY,
                    current_price=entry.entry_price,
                    ask_price=entry.entry_price,
                    atr_value=atr,
                    rsi_value=max(0.0, min(rsi, 100.0)),
                    rvol=features.rvol,
                    vwap=vwap,
                    stop_price=entry.stop_loss,
                    target_price=entry.target_2,
                    half_target=entry.target_1,
                    reclaim_candle_low=reclaim_low,
                    needs_ask_refresh=True,
                )
                signal_count += 1
                print(f"  [SIGNAL]      {signal_payload.ticker:8s} "
                      f"action={signal_payload.action!s:10s} "
                      f"entry={signal_payload.current_price:.4f}  ✓")
            except Exception as exc:
                print(f"  [SIGNAL]      {entry.symbol:8s}  ERROR: {exc}")
                errors += 1

    print()
    print(f"  POP_SIGNAL events validated : {pop_count}")
    print(f"  SIGNAL(BUY) events validated: {signal_count}")
    if errors:
        print(f"  Errors                      : {errors}  ← investigate!")
    else:
        print(f"  Errors                      : 0  ✓")


def _print_summary(trade_candidates) -> None:
    from pop_screener.models import StrategyType

    strategy_counts = {}
    pop_reason_counts = {}
    total_entries = 0
    total_exits   = 0

    for tc in trade_candidates:
        st = str(tc.strategy_assignment.primary_strategy)
        strategy_counts[st] = strategy_counts.get(st, 0) + 1
        pr = str(tc.pop_candidate.pop_reason)
        pop_reason_counts[pr] = pop_reason_counts.get(pr, 0) + 1
        total_entries += len(tc.entry_signals)
        total_exits   += len(tc.exit_signals)

    print()
    print("  Pop candidates by reason:")
    for reason, count in sorted(pop_reason_counts.items(), key=lambda x: -x[1]):
        bar = '█' * count
        print(f"    {reason:25s} {bar} ({count})")

    print()
    print("  Assigned strategies (primary):")
    for strategy, count in sorted(strategy_counts.items(), key=lambda x: -x[1]):
        bar = '█' * count
        print(f"    {strategy:30s} {bar} ({count})")

    print()
    print(f"  Total entry signals : {total_entries}")
    print(f"  Total exit signals  : {total_exits}")
    print()


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Demo for the pop-stock multi-strategy pipeline'
    )
    parser.add_argument(
        '--symbols', nargs='+',
        help='Symbols to evaluate (default: built-in 10-symbol universe)'
    )
    parser.add_argument(
        '--verbose', '-v', action='store_true',
        help='Print full feature vectors for each candidate'
    )
    args = parser.parse_args()

    # Use a larger universe when no symbols are specified
    symbols = args.symbols
    if symbols is None:
        symbols = [
            'AAPL', 'NVDA', 'TSLA', 'AMD', 'META',
            'AMZN', 'MSFT', 'GOOGL', 'COIN', 'PLTR',
            'GME',  'AMC',  'MARA',  'RIOT', 'HOOD',
            'NFLX', 'UBER', 'SNAP',  'SHOP', 'ROKU',
        ]

    main(symbols=symbols, verbose=args.verbose)
