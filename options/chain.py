"""
Alpaca options chain client for fetching and filtering option contracts.

Uses alpaca-py SDK:
  - OptionHistoricalDataClient  for snapshots (greeks, quotes)
  - TradingClient               for contract discovery via get_option_contracts()
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Alpaca SDK imports (graceful fallback)
# ---------------------------------------------------------------------------
try:
    from alpaca.data.historical.option import OptionHistoricalDataClient
    from alpaca.data.requests import OptionSnapshotRequest
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import GetOptionContractsRequest
    from alpaca.trading.enums import AssetStatus

    _HAS_ALPACA = True
except ImportError:
    _HAS_ALPACA = False
    log.warning(
        "[AlpacaOptionChainClient] alpaca-py SDK not installed; "
        "option chain queries will return empty results"
    )


class _RateLimiter:
    """Simple token-bucket rate limiter."""
    def __init__(self, max_per_second: float = 3.0):
        self._interval = 1.0 / max_per_second
        self._last_request = 0.0
        self._lock = threading.Lock()

    def wait(self):
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_request
            if elapsed < self._interval:
                time.sleep(self._interval - elapsed)
            self._last_request = time.monotonic()


@dataclass
class OptionContract:
    """Single option contract returned from Alpaca options chain."""
    symbol:       str     # OCC symbol e.g. AAPL240119C00180000
    underlying:   str     # stock symbol
    expiry_date:  date    # expiration date
    strike:       float   # strike price
    option_type:  str     # 'call' | 'put'
    delta:        float   # delta value
    gamma:        float   # gamma value
    theta:        float   # theta value
    vega:         float   # vega value
    iv:           float   # implied volatility
    bid:          float   # bid price
    ask:          float   # ask price
    dte:          int     # days to expiry


class AlpacaOptionChainClient:
    """
    Wraps Alpaca's options data API to fetch chains and find contracts.

    Uses APCA_OPTIONS_KEY credentials (not the main trading key).
    Fetches via alpaca-py SDK.
    """

    def __init__(self, api_key: str, api_secret: str) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self._data_client = None
        self._trading_client = None
        # Cache: ticker -> (timestamp, List[OptionContract])
        self._chain_cache: Dict[str, Tuple[float, List[OptionContract]]] = {}
        self._cache_ttl = 60  # seconds
        self._rate_limiter = _RateLimiter(max_per_second=3.0)

        if not _HAS_ALPACA:
            log.warning(
                "[AlpacaOptionChainClient] alpaca-py not available; "
                "clients set to None"
            )
            return

        try:
            self._data_client = OptionHistoricalDataClient(
                api_key=api_key,
                secret_key=api_secret,
            )
            self._trading_client = TradingClient(
                api_key=api_key,
                secret_key=api_secret,
            )
            log.info(
                "[AlpacaOptionChainClient] initialized with API key "
                f"***{api_key[-4:]}"
            )
        except Exception as exc:
            log.warning(
                "[AlpacaOptionChainClient] failed to create Alpaca clients: "
                f"{exc}"
            )
            self._data_client = None
            self._trading_client = None

    # ------------------------------------------------------------------
    # Cache helpers
    # ------------------------------------------------------------------
    def _cache_key(self, ticker: str, min_dte: int, max_dte: int) -> str:
        return f"{ticker}|{min_dte}|{max_dte}"

    def _get_cached(
        self, key: str
    ) -> Optional[List[OptionContract]]:
        entry = self._chain_cache.get(key)
        if entry is None:
            return None
        ts, contracts = entry
        if time.time() - ts > self._cache_ttl:
            del self._chain_cache[key]
            return None
        return contracts

    def _set_cached(self, key: str, contracts: List[OptionContract]) -> None:
        self._chain_cache[key] = (time.time(), contracts)

    # ------------------------------------------------------------------
    # Core: get_chain
    # ------------------------------------------------------------------
    def get_chain(
        self,
        ticker: str,
        min_dte: int = 20,
        max_dte: int = 45,
    ) -> List[OptionContract]:
        """
        Fetch all option contracts for ticker within the DTE window.

        Returns empty list on API error (caller must handle gracefully).
        Caches results for 60 seconds per (ticker, min_dte, max_dte).
        """
        cache_key = self._cache_key(ticker, min_dte, max_dte)
        cached = self._get_cached(cache_key)
        if cached is not None:
            log.debug(
                f"[chain] cache hit for {ticker} "
                f"({len(cached)} contracts)"
            )
            return cached

        if self._trading_client is None or self._data_client is None:
            log.warning(
                f"[chain] Alpaca clients not available for {ticker}"
            )
            return []

        try:
            return self._fetch_chain(ticker, min_dte, max_dte, cache_key)
        except Exception as exc:
            log.warning(
                f"[chain] API error fetching chain for {ticker}: {exc}"
            )
            return []

    def _fetch_chain(
        self,
        ticker: str,
        min_dte: int,
        max_dte: int,
        cache_key: str,
    ) -> List[OptionContract]:
        """Internal: fetch contracts, enrich with greeks, filter, sort."""
        today = date.today()
        exp_from = today + timedelta(days=min_dte)
        exp_to = today + timedelta(days=max_dte)

        # --- Step 1: discover contracts via TradingClient ---------------
        req = GetOptionContractsRequest(
            underlying_symbols=[ticker],
            status=AssetStatus.ACTIVE,
            expiration_date_gte=exp_from.isoformat(),
            expiration_date_lte=exp_to.isoformat(),
        )

        self._rate_limiter.wait()
        resp = self._trading_client.get_option_contracts(req)
        raw_contracts = resp.option_contracts if resp and resp.option_contracts else []

        if not raw_contracts:
            log.info(
                f"[chain] no contracts found for {ticker} "
                f"DTE {min_dte}-{max_dte}"
            )
            self._set_cached(cache_key, [])
            return []

        log.info(
            f"[chain] found {len(raw_contracts)} contracts for {ticker} "
            f"DTE {min_dte}-{max_dte}"
        )

        # --- Step 2: fetch snapshots (greeks + quotes) ------------------
        symbols = [c.symbol for c in raw_contracts]
        snapshots = self._get_snapshots_batched(symbols)

        # --- Step 3: build OptionContract list with filtering -----------
        contracts: List[OptionContract] = []
        for raw in raw_contracts:
            snap = snapshots.get(raw.symbol)
            if snap is None:
                continue

            # Extract greeks ------------------------------------------
            greeks = getattr(snap, "greeks", None)
            delta = float(greeks.delta) if greeks and greeks.delta is not None else 0.0
            gamma = float(greeks.gamma) if greeks and greeks.gamma is not None else 0.0
            theta = float(greeks.theta) if greeks and greeks.theta is not None else 0.0
            vega = float(greeks.vega) if greeks and greeks.vega is not None else 0.0
            iv = float(greeks.mid_iv) if greeks and getattr(greeks, "mid_iv", None) is not None else 0.0

            # Extract quote -------------------------------------------
            quote = getattr(snap, "latest_quote", None)
            bid = float(quote.bid_price) if quote and quote.bid_price is not None else 0.0
            ask = float(quote.ask_price) if quote and quote.ask_price is not None else 0.0

            # Liquidity filter: skip if bid is 0 or ask is 0 ----------
            if bid <= 0 or ask <= 0:
                continue

            # Liquidity filter: skip if spread > 20% of mid -----------
            # (was 50% — too wide, slippage eats all profit)
            mid = (bid + ask) / 2.0
            if mid > 0 and (ask - bid) / mid > 0.20:
                continue

            # Liquidity filter: minimum bid of $0.05 ------------------
            # (penny-wide options are effectively untradeable)
            if bid < 0.05:
                continue

            # Compute DTE ---------------------------------------------
            exp = raw.expiration_date
            if isinstance(exp, str):
                exp = date.fromisoformat(exp)
            dte = (exp - today).days

            # Map option_type to 'call'/'put' --------------------------
            otype = str(getattr(raw, "type", "")).lower()
            if "call" in otype:
                otype = "call"
            elif "put" in otype:
                otype = "put"
            else:
                continue  # unknown type, skip

            contracts.append(
                OptionContract(
                    symbol=raw.symbol,
                    underlying=ticker,
                    expiry_date=exp,
                    strike=float(raw.strike_price),
                    option_type=otype,
                    delta=delta,
                    gamma=gamma,
                    theta=theta,
                    vega=vega,
                    iv=iv,
                    bid=bid,
                    ask=ask,
                    dte=dte,
                )
            )

        # Sort by strike -----------------------------------------------
        contracts.sort(key=lambda c: c.strike)

        log.info(
            f"[chain] {len(contracts)} liquid contracts after filtering "
            f"for {ticker}"
        )
        self._set_cached(cache_key, contracts)
        return contracts

    def _get_snapshots_batched(
        self, symbols: List[str], batch_size: int = 100
    ) -> Dict:
        """
        Fetch option snapshots in batches to respect API limits.

        Returns dict of symbol -> snapshot.
        """
        all_snapshots: Dict = {}
        for i in range(0, len(symbols), batch_size):
            batch = symbols[i : i + batch_size]
            for attempt in range(3):
                try:
                    self._rate_limiter.wait()
                    req = OptionSnapshotRequest(symbol_or_symbols=batch)
                    snaps = self._data_client.get_option_snapshot(req)
                    if snaps:
                        all_snapshots.update(snaps)
                    break  # success
                except Exception as exc:
                    exc_str = str(exc)
                    if '429' in exc_str and attempt < 2:
                        backoff = 2 ** (attempt + 1)
                        log.warning(
                            "[chain] 429 rate limited (batch %d, attempt %d) — "
                            "retrying in %ds", i, attempt + 1, backoff,
                        )
                        time.sleep(backoff)
                        continue
                    log.warning(
                        f"[chain] snapshot batch error (batch {i}): {exc}"
                    )
                    break
        return all_snapshots

    # ------------------------------------------------------------------
    # get_greeks — fetch current Greeks for open position legs
    # ------------------------------------------------------------------
    def get_greeks(
        self, legs
    ) -> List[Dict]:
        """Fetch current Greeks for a list of option contract legs.

        Args:
            legs: list of OptionLeg objects (or dicts) with 'symbol', 'qty',
                  'side' attributes/keys.

        Returns list of dicts with delta, gamma, theta, vega, qty, side.
        Returns empty list on failure or if data client is unavailable.
        """
        if self._data_client is None:
            return []

        symbols = []
        leg_meta = []  # parallel list: (qty, side) per symbol
        for leg in legs:
            sym = leg.symbol if hasattr(leg, 'symbol') else leg['symbol']
            qty = leg.qty if hasattr(leg, 'qty') else leg['qty']
            side = leg.side if hasattr(leg, 'side') else leg['side']
            symbols.append(sym)
            leg_meta.append((qty, side))

        if not symbols:
            return []

        # Check cache (keyed by frozenset of symbols)
        cache_key = "_greeks|" + "|".join(sorted(symbols))
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        # Fetch snapshots for all leg symbols in one batch
        try:
            snapshots = self._get_snapshots_batched(symbols)
        except Exception as exc:
            log.warning("[chain] get_greeks snapshot error: %s", exc)
            return []

        results: List[Dict] = []
        for sym, (qty, side) in zip(symbols, leg_meta):
            snap = snapshots.get(sym)
            if snap is None:
                continue
            greeks = getattr(snap, "greeks", None)
            if greeks is None:
                continue

            results.append({
                'delta': float(greeks.delta) if greeks.delta is not None else 0.0,
                'gamma': float(greeks.gamma) if greeks.gamma is not None else 0.0,
                'theta': float(greeks.theta) if greeks.theta is not None else 0.0,
                'vega':  float(greeks.vega) if greeks.vega is not None else 0.0,
                'qty':   qty,
                'side':  side,
            })

        # Cache with standard TTL (60s)
        self._set_cached(cache_key, results)
        return results

    # ------------------------------------------------------------------
    # get_greeks_for_symbol — single-contract Greeks lookup
    # ------------------------------------------------------------------
    def get_greeks_for_symbol(self, symbol: str) -> Optional[Dict]:
        """Fetch current Greeks for a single option contract symbol.

        Returns dict with delta, gamma, theta, vega keys, or None on failure.
        Used by PortfolioGreeksTracker.
        """
        if self._data_client is None:
            return None

        try:
            snapshots = self._get_snapshots_batched([symbol])
            snap = snapshots.get(symbol)
            if snap is None:
                return None

            greeks = getattr(snap, "greeks", None)
            if greeks is None:
                return None

            return {
                'delta': float(greeks.delta) if greeks.delta is not None else 0.0,
                'gamma': float(greeks.gamma) if greeks.gamma is not None else 0.0,
                'theta': float(greeks.theta) if greeks.theta is not None else 0.0,
                'vega':  float(greeks.vega) if greeks.vega is not None else 0.0,
            }
        except Exception as exc:
            log.warning("[chain] get_greeks_for_symbol error for %s: %s", symbol, exc)
            return None

    # ------------------------------------------------------------------
    # find_by_delta / find_atm / find_otm (unchanged)
    # ------------------------------------------------------------------
    def find_by_delta(
        self,
        contracts: List[OptionContract],
        option_type: str,         # 'call' | 'put'
        target_delta: float,      # e.g. 0.35
        tolerance: float = 0.05,
    ) -> Optional[OptionContract]:
        """
        Select the contract whose delta is closest to target_delta within tolerance.

        Returns None if no suitable contract found.
        """
        if not contracts:
            return None

        candidates = [
            c for c in contracts
            if c.option_type == option_type
            and abs(c.delta - target_delta) <= tolerance
        ]
        if not candidates:
            return None

        # Return closest match
        return min(candidates, key=lambda c: abs(c.delta - target_delta))

    def find_atm(
        self,
        contracts: List[OptionContract],
        option_type: str,
        underlying_price: float,
    ) -> Optional[OptionContract]:
        """Select the contract with strike closest to underlying_price."""
        if not contracts:
            return None

        candidates = [c for c in contracts if c.option_type == option_type]
        if not candidates:
            return None

        return min(candidates, key=lambda c: abs(c.strike - underlying_price))

    def find_otm(
        self,
        contracts: List[OptionContract],
        option_type: str,
        underlying_price: float,
        otm_pct: float = 0.03,     # how far OTM as fraction of price
    ) -> Optional[OptionContract]:
        """
        Select contract approximately otm_pct% OTM from underlying_price.

        For calls: strike = underlying * (1 + otm_pct).
        For puts:  strike = underlying * (1 - otm_pct).
        """
        if not contracts:
            return None

        if option_type == 'call':
            target_strike = underlying_price * (1 + otm_pct)
        elif option_type == 'put':
            target_strike = underlying_price * (1 - otm_pct)
        else:
            return None

        candidates = [c for c in contracts if c.option_type == option_type]
        if not candidates:
            return None

        return min(candidates, key=lambda c: abs(c.strike - target_strike))

    # ------------------------------------------------------------------
    # find_leaps
    # ------------------------------------------------------------------
    def find_leaps(
        self,
        ticker: str,
        option_type: str,
        target_delta: float,
        min_dte: int = 300,
    ) -> Optional[OptionContract]:
        """
        Fetch a LEAPS contract for calendar/diagonal spread long leg.

        Searches for contracts with DTE >= min_dte and selects the one
        closest to target_delta.
        """
        if self._trading_client is None or self._data_client is None:
            log.warning(
                f"[chain] Alpaca clients not available for LEAPS {ticker}"
            )
            return None

        try:
            return self._fetch_leaps(ticker, option_type, target_delta, min_dte)
        except Exception as exc:
            log.warning(
                f"[chain] API error fetching LEAPS for {ticker}: {exc}"
            )
            return None

    def _fetch_leaps(
        self,
        ticker: str,
        option_type: str,
        target_delta: float,
        min_dte: int,
    ) -> Optional[OptionContract]:
        """Internal: fetch LEAPS contracts and find by delta."""
        # Use a generous DTE window for the chain fetch and reuse caching
        max_dte = min_dte + 365  # look up to ~1 year past min_dte
        contracts = self.get_chain(ticker, min_dte=min_dte, max_dte=max_dte)
        if not contracts:
            log.info(f"[chain] no LEAPS contracts found for {ticker}")
            return None

        result = self.find_by_delta(
            contracts,
            option_type=option_type,
            target_delta=target_delta,
            tolerance=0.10,  # wider tolerance for LEAPS
        )
        if result:
            log.info(
                f"[chain] LEAPS found: {result.symbol} "
                f"delta={result.delta:.2f} DTE={result.dte}"
            )
        else:
            log.info(
                f"[chain] no LEAPS matching delta={target_delta} "
                f"for {ticker}"
            )
        return result

    # ------------------------------------------------------------------
    # get_quote
    # ------------------------------------------------------------------
    def get_quote(
        self, symbol: str
    ) -> Optional[Tuple[float, float]]:
        """
        Get current bid/ask for a specific option symbol.

        Used by exit management to check current position value.

        Returns (bid, ask) tuple or None on error / no data.
        """
        if self._data_client is None:
            log.warning(
                f"[chain] data client not available for quote {symbol}"
            )
            return None

        try:
            self._rate_limiter.wait()
            req = OptionSnapshotRequest(symbol_or_symbols=symbol)
            snaps = self._data_client.get_option_snapshot(req)
            if not snaps or symbol not in snaps:
                log.warning(f"[chain] no snapshot for {symbol}")
                return None

            snap = snaps[symbol]
            quote = getattr(snap, "latest_quote", None)
            if quote is None:
                log.warning(f"[chain] no quote in snapshot for {symbol}")
                return None

            bid = float(quote.bid_price) if quote.bid_price is not None else 0.0
            ask = float(quote.ask_price) if quote.ask_price is not None else 0.0
            return (bid, ask)
        except Exception as exc:
            log.warning(f"[chain] error fetching quote for {symbol}: {exc}")
            return None
