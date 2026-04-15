"""Detect unusual options activity as a signal source for equity trades."""
import logging
import time

log = logging.getLogger(__name__)


class UnusualOptionsFlowScanner:
    """Detect unusual options activity — high volume relative to open interest.

    Institutional traders often use options markets to establish large
    positions before equity moves. Detecting this flow provides edge.
    """

    def __init__(self, chain_client=None, volume_oi_threshold: float = 3.0):
        """
        Args:
            chain_client: AlpacaOptionChainClient or None (disabled if None)
            volume_oi_threshold: volume/OI ratio to flag as unusual (default 3x)
        """
        self._chain = chain_client
        self._threshold = volume_oi_threshold
        self._cache = {}
        self._cache_ttl = 300  # 5 minutes
        self._scan_index = 0   # for rotating through universe

    def scan(self, ticker: str) -> dict | None:
        """Scan a ticker for unusual options activity.

        Returns dict if unusual activity detected, None otherwise.
        """
        if self._chain is None:
            return None

        # Check cache
        now = time.time()
        if ticker in self._cache and (now - self._cache[ticker].get('ts', 0)) < self._cache_ttl:
            cached = self._cache[ticker]
            return cached.get('result')

        try:
            chain = self._chain.get_chain(ticker)
            if not chain:
                self._cache[ticker] = {'ts': now, 'result': None}
                return None

            call_volume = 0
            put_volume = 0
            call_oi = 0
            put_oi = 0
            largest_trade = None
            largest_vol = 0

            for contract in chain:
                vol = getattr(contract, 'volume', 0) or 0
                oi = getattr(contract, 'open_interest', 0) or 0
                opt_type = getattr(contract, 'option_type', '') or ''

                if 'call' in str(opt_type).lower():
                    call_volume += vol
                    call_oi += oi
                else:
                    put_volume += vol
                    put_oi += oi

                if vol > largest_vol:
                    largest_vol = vol
                    largest_trade = {
                        'strike': getattr(contract, 'strike', 0),
                        'expiry': str(getattr(contract, 'expiry', '')),
                        'type': opt_type,
                        'volume': vol,
                    }

            total_volume = call_volume + put_volume
            total_oi = call_oi + put_oi

            if total_oi == 0 or total_volume == 0:
                self._cache[ticker] = {'ts': now, 'result': None}
                return None

            volume_oi_ratio = total_volume / total_oi

            if volume_oi_ratio < self._threshold:
                self._cache[ticker] = {'ts': now, 'result': None}
                return None

            # Determine signal direction
            if call_volume > 2 * put_volume:
                signal = 'bullish'
            elif put_volume > 2 * call_volume:
                signal = 'bearish'
            else:
                signal = 'neutral'

            put_call_ratio = put_volume / call_volume if call_volume > 0 else float('inf')
            confidence = min(1.0, volume_oi_ratio / (self._threshold * 2))

            result = {
                'ticker': ticker,
                'signal': signal,
                'call_volume': call_volume,
                'put_volume': put_volume,
                'call_oi': call_oi,
                'put_oi': put_oi,
                'volume_oi_ratio': round(volume_oi_ratio, 2),
                'put_call_ratio': round(put_call_ratio, 2),
                'largest_trade': largest_trade,
                'confidence': round(confidence, 3),
            }

            self._cache[ticker] = {'ts': now, 'result': result}
            log.info("[UOF] Unusual activity: %s %s (vol/oi=%.1fx, confidence=%.2f)",
                     ticker, signal, volume_oi_ratio, confidence)
            return result

        except Exception as exc:
            log.debug("Options flow scan failed for %s: %s", ticker, exc)
            self._cache[ticker] = {'ts': now, 'result': None}
            return None

    def scan_universe(self, tickers: list, max_per_cycle: int = 20) -> list:
        """Scan a batch of tickers, return those with unusual activity.

        Rotates through the universe to stay within API rate limits.
        """
        # Take a slice of the universe, rotating each call
        start = self._scan_index % len(tickers) if tickers else 0
        batch = tickers[start:start + max_per_cycle]
        if len(batch) < max_per_cycle:
            batch += tickers[:max_per_cycle - len(batch)]
        self._scan_index = (start + max_per_cycle) % max(len(tickers), 1)

        results = []
        for ticker in batch:
            result = self.scan(ticker)
            if result is not None:
                results.append(result)

        return results
