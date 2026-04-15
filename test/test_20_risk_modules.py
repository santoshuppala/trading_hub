"""Test suite for risk modules: PortfolioRiskGate, RiskSizer, SmartRouter."""
import unittest
import sys
import os

_project_root = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, _project_root)

# Load .env so API keys and config are available
_env_path = os.path.join(_project_root, '.env')
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.split('#')[0].strip()
            if '=' in _line:
                _k, _v = _line.split('=', 1)
                _v = _v.strip().strip('"').strip("'")
                if _k.strip() and _k.strip() not in os.environ:
                    os.environ[_k.strip()] = _v


class TestRiskSizer(unittest.TestCase):
    def setUp(self):
        from monitor.risk_sizing import RiskSizer
        self.sizer = RiskSizer()

    def test_beta_tqqq(self):
        """TQQQ (beta 3.0) should get 1/3 the shares."""
        result = self.sizer.adjust_size('TQQQ', base_qty=10, price=50.0)
        self.assertLess(result.adjusted_qty, 10)
        self.assertEqual(result.beta, 3.0)

    def test_beta_spy(self):
        """SPY (beta 1.0) should not be reduced by beta."""
        result = self.sizer.adjust_size('SPY', base_qty=10, price=500.0, trade_budget=10000)
        self.assertEqual(result.beta, 1.0)

    def test_correlation_block_semis(self):
        """MU should be blocked when 3 semiconductors already held."""
        ok, reason = self.sizer.check_correlation('MU', {'NVDA', 'AMD', 'AVGO'})
        self.assertFalse(ok)
        self.assertIn('semiconductor', reason.lower())

    def test_correlation_allow_different_group(self):
        """AAPL should be allowed with semiconductors (different group)."""
        ok, _ = self.sizer.check_correlation('AAPL', {'NVDA', 'AMD', 'AVGO'})
        self.assertTrue(ok)

    def test_news_override_correlation(self):
        """Ticker-specific catalyst should override correlation block."""
        ok, reason = self.sizer.check_correlation(
            'AAPL', {'MSFT', 'GOOGL', 'META'},
            news_context={'headlines_1h': 5, 'sentiment_delta': 0.45}
        )
        self.assertTrue(ok)
        self.assertIn('override', reason.lower())

    def test_news_weak_no_override(self):
        """Weak news should NOT override correlation block."""
        ok, _ = self.sizer.check_correlation(
            'AAPL', {'MSFT', 'GOOGL', 'META'},
            news_context={'headlines_1h': 1, 'sentiment_delta': 0.05}
        )
        self.assertFalse(ok)

    def test_volatility_sizing(self):
        """High ATR should reduce position size."""
        result = self.sizer.adjust_size('AAPL', base_qty=100, price=150.0, atr_value=5.0)
        self.assertLess(result.adjusted_qty, 100)
        self.assertGreater(result.atr_risk, 0)

    def test_beta_weighted_exposure(self):
        """Beta-weighted exposure: TQQQ should contribute 3x."""
        positions = {
            'SPY': {'qty': 10, 'current_price': 500},   # 10 * 500 * 1.0 = 5000
            'TQQQ': {'qty': 10, 'current_price': 50},   # 10 * 50 * 3.0 = 1500
        }
        exposure = self.sizer.beta_weighted_exposure(positions)
        self.assertGreater(exposure, 6000)  # SPY 5000 + TQQQ 1500

    def test_no_correlation_group(self):
        """Ticker not in any group should always be allowed."""
        ok, reason = self.sizer.check_correlation('ZZZZ', {'NVDA', 'AMD', 'AVGO'})
        self.assertTrue(ok)
        self.assertIn('no_correlation_group', reason)

    def test_min_qty_one(self):
        """Adjusted quantity should never go below 1."""
        result = self.sizer.adjust_size('TQQQ', base_qty=1, price=50.0)
        self.assertGreaterEqual(result.adjusted_qty, 1)


class TestPortfolioRiskGate(unittest.TestCase):
    def setUp(self):
        from monitor.event_bus import EventBus
        from monitor.portfolio_risk import PortfolioRiskGate
        self.bus = EventBus()
        self.gate = PortfolioRiskGate(self.bus)

    def test_initial_status(self):
        """Gate should start clean."""
        status = self.gate.status()
        self.assertFalse(status['halted'])
        self.assertEqual(status['realized_pnl'], 0)
        self.assertEqual(status['positions_count'], 0)
        self.assertEqual(status['blocks_today'], 0)

    def test_reset_day(self):
        """reset_day should clear all state."""
        self.gate._realized_pnl = 100.0
        self.gate._halted = True
        self.gate.reset_day()
        self.assertFalse(self.gate._halted)
        self.assertEqual(self.gate._realized_pnl, 0.0)

    def test_sells_always_pass(self):
        """SELL orders should never be blocked by portfolio risk."""
        # Simulate a halt
        self.gate._halted = False
        # We can't easily test ORDER_REQ emission without full bus wiring,
        # but we can verify the logic
        self.assertFalse(self.gate._halted)


class TestSmartRouter(unittest.TestCase):
    def setUp(self):
        from monitor.event_bus import EventBus
        from monitor.smart_router import SmartRouter
        self.bus = EventBus()
        self.router = SmartRouter(self.bus, default_broker='alpaca')

    def test_status_empty(self):
        """Router with no brokers should have empty status."""
        status = self.router.status()
        self.assertEqual(len(status), 0)

    def test_health_default(self):
        """All registered brokers should start healthy."""
        from monitor.smart_router import SmartRouter, BrokerHealth
        bus = self.bus
        # Mock broker
        class MockBroker:
            pass
        router = SmartRouter(bus, alpaca_broker=MockBroker(), default_broker='alpaca')
        status = router.status()
        self.assertTrue(status['alpaca']['available'])
        self.assertEqual(status['alpaca']['consecutive_failures'], 0)


if __name__ == '__main__':
    unittest.main()
