#!/usr/bin/env python
"""
Basic smoke test for backtesting framework.
Verifies that the core components can be instantiated.
"""
import logging
from datetime import datetime

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


def test_imports():
    """Test that all modules can be imported."""
    try:
        from backtests.sync_bus import BacktestBus
        from backtests.fill_simulator import FillSimulator
        from backtests.data_loader import BarDataLoader
        from backtests.metrics import MetricsEngine
        from backtests.adapters.pro_adapter import ProBacktestAdapter
        from backtests.engine import BacktestEngine
        from backtests.mocks.options_chain import SyntheticOptionChainClient
        log.info("✓ All imports successful")
        return True
    except ImportError as e:
        log.error(f"✗ Import failed: {e}")
        return False


def test_sync_bus():
    """Test BacktestBus instantiation."""
    try:
        from backtests.sync_bus import BacktestBus
        bus = BacktestBus()
        assert bus.bus is not None
        assert bus.capture is not None
        log.info("✓ BacktestBus instantiated successfully")
        return True
    except Exception as e:
        log.error(f"✗ BacktestBus test failed: {e}")
        return False


def test_fill_simulator():
    """Test FillSimulator instantiation."""
    try:
        from backtests.fill_simulator import FillSimulator
        fill_sim = FillSimulator(trade_budget=1000.0)
        assert fill_sim.trade_budget == 1000.0
        assert fill_sim.current_equity == 1000.0
        log.info("✓ FillSimulator instantiated successfully")
        return True
    except Exception as e:
        log.error(f"✗ FillSimulator test failed: {e}")
        return False


def test_data_loader():
    """Test BarDataLoader instantiation."""
    try:
        from backtests.data_loader import BarDataLoader
        loader = BarDataLoader(source='yfinance')
        log.info("✓ BarDataLoader instantiated successfully")
        return True
    except Exception as e:
        log.error(f"✗ BarDataLoader test failed: {e}")
        return False


def test_metrics_engine():
    """Test MetricsEngine instantiation."""
    try:
        from backtests.metrics import MetricsEngine
        metrics = MetricsEngine()
        assert metrics.equity_curve == []
        log.info("✓ MetricsEngine instantiated successfully")
        return True
    except Exception as e:
        log.error(f"✗ MetricsEngine test failed: {e}")
        return False


def test_options_chain():
    """Test SyntheticOptionChainClient instantiation."""
    try:
        from backtests.mocks.options_chain import SyntheticOptionChainClient
        chain = SyntheticOptionChainClient()
        chain.update_bar('AAPL', atr=2.5, spot=150.0)
        contracts = chain.get_chain('AAPL', min_dte=20, max_dte=45)
        assert isinstance(contracts, list)
        log.info(f"✓ SyntheticOptionChainClient instantiated and generated {len(contracts)} contracts")
        return True
    except Exception as e:
        log.error(f"✗ SyntheticOptionChainClient test failed: {e}")
        return False


if __name__ == '__main__':
    tests = [
        test_imports,
        test_sync_bus,
        test_fill_simulator,
        test_data_loader,
        test_metrics_engine,
        test_options_chain,
    ]

    results = [test() for test in tests]

    print("\n" + "=" * 60)
    print(f"RESULTS: {sum(results)}/{len(results)} tests passed")
    print("=" * 60)

    if not all(results):
        exit(1)
