"""Test suite for data source adapters and smart persistence."""
import unittest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


class TestYahooFinance(unittest.TestCase):
    def setUp(self):
        from data_sources.yahoo_finance import YahooFinanceSource
        self.yf = YahooFinanceSource()

    def test_fundamentals_aapl(self):
        """AAPL should return valid fundamentals."""
        f = self.yf.fundamentals('AAPL')
        self.assertIsNotNone(f)
        self.assertGreater(f.market_cap, 1e12)  # > $1T
        self.assertGreater(f.pe_ratio, 0)
        self.assertGreater(f.beta, 0)

    def test_analyst_consensus(self):
        """AAPL should have analyst data."""
        a = self.yf.analyst_consensus('AAPL')
        self.assertIsNotNone(a)
        self.assertGreater(a.target_mean, 0)

    def test_unknown_ticker(self):
        """Unknown ticker should return None or empty, not crash."""
        f = self.yf.fundamentals('ZZZZZZ123')
        # May return None or an object with zero values — just shouldn't crash


class TestFearGreed(unittest.TestCase):
    def test_current_value(self):
        """Fear & Greed should return value in 0-100."""
        from data_sources.fear_greed import FearGreedSource
        fg = FearGreedSource()
        current = fg.get_current()
        if current:  # may fail if CNN endpoint is down
            self.assertGreaterEqual(current['value'], 0)
            self.assertLessEqual(current['value'], 100)
            self.assertIn(current['label'], [
                'Extreme Fear', 'Fear', 'Neutral', 'Greed', 'Extreme Greed'
            ])

    def test_regime(self):
        """Regime should return a valid string."""
        from data_sources.fear_greed import FearGreedSource
        fg = FearGreedSource()
        regime = fg.regime()
        self.assertIn(regime, ['extreme_fear', 'fear', 'neutral', 'greed', 'extreme_greed'])


class TestFREDMacro(unittest.TestCase):
    def test_no_key_graceful(self):
        """FRED without key should return defaults, not crash."""
        from data_sources.fred_macro import FREDMacroSource
        fred = FREDMacroSource(api_key='')
        regime = fred.macro_regime()
        self.assertIn(regime.regime, ['risk_on', 'neutral', 'risk_off', 'crisis'])

    def test_with_key(self):
        """FRED with key should return real data."""
        from data_sources.fred_macro import FREDMacroSource
        key = os.getenv('FRED_API_KEY', '')
        if not key:
            self.skipTest('FRED_API_KEY not set')
        fred = FREDMacroSource(api_key=key)
        rate = fred.fed_funds_rate()
        self.assertIsNotNone(rate)
        self.assertGreater(rate, 0)


class TestSmartPersistence(unittest.TestCase):
    def setUp(self):
        from data_sources.persistence import SmartPersistence
        self.writes = []
        self.sp = SmartPersistence(writer_fn=lambda *args: self.writes.append(args))

    def test_first_arrival_persists(self):
        """First data for a key should always persist."""
        self.sp.persist_if_changed('test', 'AAPL', {'value': 100})
        self.assertEqual(len(self.writes), 1)

    def test_same_data_skipped(self):
        """Identical data should be skipped."""
        self.sp.persist_if_changed('test', 'AAPL', {'value': 100})
        self.sp.persist_if_changed('test', 'AAPL', {'value': 100})
        self.assertEqual(len(self.writes), 1)

    def test_changed_data_persists(self):
        """Changed data should persist."""
        self.sp.persist_if_changed('test', 'AAPL', {'value': 100})
        self.sp.persist_if_changed('test', 'AAPL', {'value': 200})
        self.assertEqual(len(self.writes), 2)

    def test_fear_greed_threshold(self):
        """Fear & Greed change < 2 points should be skipped."""
        from data_sources.persistence import fear_greed_changed
        self.sp.persist_if_changed('fg', 'MKT', {'value': 50.0}, fear_greed_changed)
        self.sp.persist_if_changed('fg', 'MKT', {'value': 51.0}, fear_greed_changed)  # skip
        self.sp.persist_if_changed('fg', 'MKT', {'value': 55.0}, fear_greed_changed)  # persist
        self.assertEqual(len(self.writes), 2)  # first + big change

    def test_stats(self):
        """Stats should track persisted and skipped counts."""
        self.sp.persist_if_changed('test', 'A', {'v': 1})
        self.sp.persist_if_changed('test', 'A', {'v': 1})
        stats = self.sp.stats()
        self.assertEqual(stats['persisted'], 1)
        self.assertEqual(stats['skipped'], 1)

    def test_reset_day(self):
        """After reset, same data should persist again."""
        self.sp.persist_if_changed('test', 'AAPL', {'value': 100})
        self.sp.reset_day()
        self.sp.persist_if_changed('test', 'AAPL', {'value': 100})
        self.assertEqual(len(self.writes), 2)


class TestCollectorCircuitBreaker(unittest.TestCase):
    def test_source_disabled_after_failures(self):
        """Source should be disabled after 3 consecutive failures."""
        from data_sources.collector import DataSourceCollector
        collector = DataSourceCollector()
        # Simulate failures
        collector._record_source_failure('test_source')
        collector._record_source_failure('test_source')
        self.assertTrue(collector._is_source_healthy('test_source'))
        collector._record_source_failure('test_source')  # 3rd failure
        self.assertFalse(collector._is_source_healthy('test_source'))

    def test_source_recovers(self):
        """Source should recover after success."""
        from data_sources.collector import DataSourceCollector
        collector = DataSourceCollector()
        collector._record_source_failure('test_source')
        collector._record_source_failure('test_source')
        collector._record_source_success('test_source')
        self.assertEqual(collector._source_failures.get('test_source', 0), 0)


if __name__ == '__main__':
    unittest.main()
