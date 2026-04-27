"""
OptionsLifecycleAdapter — maps OptionsEngine state to lifecycle interface.

Options has its own Alpaca account (APCA_OPTIONS_KEY / APCA_OPTIONS_SECRET).
Most complex state: positions with legs/greeks, portfolio-level Greeks, risk gate.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

from .base import AbstractEngineAdapter

log = logging.getLogger(__name__)


class OptionsLifecycleAdapter(AbstractEngineAdapter):

    def __init__(self, options_engine):
        """
        Args:
            options_engine: OptionsEngine instance
        """
        self._engine = options_engine

    def get_state(self) -> dict:
        # Serialize positions (OptionsPosition objects → dicts)
        positions = {}
        with self._engine._positions_lock:
            for ticker, pos in self._engine._positions.items():
                positions[ticker] = {
                    'strategy_type': pos.strategy_type,
                    'entry_cost': pos.entry_cost,
                    'max_risk': pos.max_risk,
                    'max_reward': pos.max_reward,
                    'expiry_date': str(pos.expiry_date),
                    'entry_time': pos.entry_time,
                }

        risk = self._engine._risk
        risk_state = {
            'open_positions': dict(risk._open_positions),
            'deployed_capital': risk._deployed_capital,
            'daily_trade_count': risk._daily_trade_count,
        }

        return {
            'positions': positions,
            'daily_pnl': self._engine._daily_pnl,
            'daily_entries': self._engine._daily_entries,
            'daily_exits': self._engine._daily_exits,
            'portfolio_greeks': {
                'delta': self._engine._portfolio_delta,
                'theta': self._engine._portfolio_theta,
                'vega': self._engine._portfolio_vega,
            },
            'risk_gate': risk_state,
            'trade_log': self._get_trade_log_from_engine(),
        }

    def restore_state(self, state: dict) -> None:
        # Restore daily counters
        self._engine._daily_pnl = state.get('daily_pnl', 0.0)
        self._engine._daily_entries = state.get('daily_entries', 0)
        self._engine._daily_exits = state.get('daily_exits', 0)

        # Restore portfolio Greeks
        greeks = state.get('portfolio_greeks', {})
        self._engine._portfolio_delta = greeks.get('delta', 0.0)
        self._engine._portfolio_theta = greeks.get('theta', 0.0)
        self._engine._portfolio_vega = greeks.get('vega', 0.0)

        # Restore risk gate state
        risk_state = state.get('risk_gate', {})
        risk = self._engine._risk
        with risk._lock:
            risk._open_positions = risk_state.get('open_positions', {})
            risk._deployed_capital = risk_state.get('deployed_capital', 0.0)
            risk._daily_trade_count = risk_state.get('daily_trade_count', 0)

        # Note: We do NOT restore _positions (OptionsPosition objects) from state file.
        # Instead, reconciliation with broker will rebuild actual positions.
        # The state file positions are just for reference/logging.

        positions = state.get('positions', {})
        log.info("[options] Restored state: %d position refs, %d trades, "
                 "pnl=$%.2f, deployed=$%.2f",
                 len(positions), self._engine._daily_entries,
                 self._engine._daily_pnl, risk._deployed_capital)

    def get_positions(self) -> Dict[str, Any]:
        with self._engine._positions_lock:
            return {t: {'strategy': p.strategy_type, 'max_risk': p.max_risk}
                    for t, p in self._engine._positions.items()}

    def get_trade_log(self) -> List[dict]:
        return self._get_trade_log_from_engine()

    def _get_trade_log_from_engine(self) -> List[dict]:
        """Options engine doesn't maintain a trade_log list like Core.
        We track closes via daily_exits counter. For EOD report, query DB."""
        return []  # TODO: query from event_store if needed

    def get_daily_pnl(self) -> float:
        return self._engine._daily_pnl

    def get_daily_stats(self) -> dict:
        return {
            'trades': self._engine._daily_entries,
            'wins': 0,  # Options doesn't track wins separately
            'pnl': self._engine._daily_pnl,
            'open_positions': len(self._engine._positions),
            'extra': {
                'entries': self._engine._daily_entries,
                'exits': self._engine._daily_exits,
                'portfolio_delta': round(self._engine._portfolio_delta, 2),
                'portfolio_theta': round(self._engine._portfolio_theta, 2),
                'portfolio_vega': round(self._engine._portfolio_vega, 2),
                'deployed_capital': round(self._engine._risk._deployed_capital, 2),
            },
        }

    def get_broker_positions(self) -> Dict[str, int]:
        """Query Options Alpaca account for open positions."""
        try:
            from alpaca.trading.client import TradingClient
            key = os.getenv('APCA_OPTIONS_KEY', '')
            secret = os.getenv('APCA_OPTIONS_SECRET', '')
            if not key or not secret:
                return {}
            client = TradingClient(key, secret, paper=True)
            positions = client.get_all_positions()
            # Group option legs by underlying ticker
            from collections import defaultdict
            by_ticker = defaultdict(int)
            for p in positions:
                sym = str(p.symbol)
                # Extract underlying from option symbol (AAPL260508C00265000 → AAPL)
                for i, ch in enumerate(sym):
                    if ch.isdigit():
                        ticker = sym[:i]
                        break
                else:
                    ticker = sym
                by_ticker[ticker] += 1
            return dict(by_ticker)
        except Exception as exc:
            log.warning("[options] Broker position fetch failed: %s", exc)
            return {}

    def force_close_all(self, reason: str) -> None:
        """Close all options positions via OptionsEngine.close_all_positions()."""
        try:
            self._engine.close_all_positions(reason=reason)
        except Exception as exc:
            log.error("[options] Force close failed: %s", exc)

    def cancel_stale_orders(self) -> int:
        """Cancel stale orders on Options Alpaca account."""
        try:
            from alpaca.trading.client import TradingClient
            from alpaca.trading.requests import GetOrdersRequest
            from alpaca.trading.enums import QueryOrderStatus
            key = os.getenv('APCA_OPTIONS_KEY', '')
            secret = os.getenv('APCA_OPTIONS_SECRET', '')
            if not key or not secret:
                return 0
            client = TradingClient(key, secret, paper=True)
            orders = client.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN))
            cancelled = 0
            for o in orders:
                try:
                    client.cancel_order_by_id(str(o.id))
                    cancelled += 1
                except Exception:
                    pass
            return cancelled
        except Exception:
            return 0

    def verify_connectivity(self) -> bool:
        """Check Options Alpaca account is reachable."""
        try:
            from alpaca.trading.client import TradingClient
            key = os.getenv('APCA_OPTIONS_KEY', '')
            secret = os.getenv('APCA_OPTIONS_SECRET', '')
            if not key or not secret:
                return False
            client = TradingClient(key, secret, paper=True)
            client.get_account()
            return True
        except Exception:
            return False

    def add_position(self, ticker: str, info: Any) -> None:
        """Rebuild orphaned option position from broker contracts.

        Reverse-engineers the spread structure from individual OCC symbols
        returned by Alpaca, infers strategy type, and creates a full
        OptionsPosition + lifecycle so the position is monitored.
        """
        try:
            positions = self._fetch_broker_legs(ticker)
            if not positions:
                log.warning("[options] Orphan %s: no legs found at broker", ticker)
                return

            from options.strategies.base import OptionsTradeSpec, OptionLeg
            from options.engine import OptionsPosition
            from options.options_exit_engine import OptionsPositionLifecycle
            import time as _time

            # Parse OCC symbols → legs with strike/type/side
            legs = []
            short_strikes = []
            long_strikes = []
            expiry_date = ''
            total_cost = 0.0

            for p in positions:
                symbol = str(p.symbol)
                qty = int(p.qty)  # positive=long, negative=short
                cost_basis = float(getattr(p, 'cost_basis', 0) or 0)
                total_cost += cost_basis

                # Parse OCC: AAPL260515C00180000
                try:
                    strike = int(symbol[-8:]) / 1000.0
                except (ValueError, IndexError):
                    log.warning("[options] Orphan %s: bad OCC symbol %s", ticker, symbol)
                    continue

                side = 'buy' if qty > 0 else 'sell'
                abs_qty = abs(qty)

                legs.append(OptionLeg(
                    symbol=symbol, side=side, qty=abs_qty,
                    ratio=1, limit_price=0.0))

                if side == 'sell':
                    short_strikes.append(strike)
                else:
                    long_strikes.append(strike)

                if not expiry_date and len(symbol) > 15:
                    # Extract expiry from OCC: positions 4-10 = YYMMDD
                    try:
                        # Find where digits start after ticker
                        for i, ch in enumerate(symbol):
                            if ch.isdigit():
                                yy = symbol[i:i+2]
                                mm = symbol[i+2:i+4]
                                dd = symbol[i+4:i+6]
                                expiry_date = f'20{yy}-{mm}-{dd}'
                                break
                    except Exception:
                        pass

            if not legs:
                log.warning("[options] Orphan %s: no valid legs parsed", ticker)
                return

            # Infer strategy type from leg structure
            strategy_type = self._infer_strategy(legs, short_strikes, long_strikes)

            # Estimate risk/reward from spread structure
            net_debit = total_cost  # cost_basis is what we paid (positive=debit, negative=credit)
            if short_strikes and long_strikes:
                spread_width = max(
                    max(abs(s - l) for s in short_strikes for l in long_strikes),
                    0.01) * 100 * (legs[0].qty if legs else 1)
                if net_debit < 0:
                    # Credit spread: max_risk = spread_width - credit, max_reward = credit
                    max_reward = abs(net_debit)
                    max_risk = spread_width - max_reward
                else:
                    # Debit spread: max_risk = debit, max_reward = spread_width - debit
                    max_risk = abs(net_debit)
                    max_reward = spread_width - max_risk
            else:
                # Single leg: max_risk = cost, max_reward = unlimited (cap at 10x)
                max_risk = abs(net_debit) if net_debit > 0 else abs(net_debit) * 5
                max_reward = abs(net_debit) * 10

            max_risk = max(max_risk, 1.0)
            max_reward = max(max_reward, 1.0)

            # Get current underlying price
            spot = 0.0
            try:
                if self._engine._chain:
                    contracts = self._engine._chain.get_chain(ticker, min_dte=1, max_dte=500)
                    if contracts:
                        spot = getattr(contracts[0], 'underlying_price', 0) or 0
            except Exception:
                pass
            if spot <= 0:
                # Estimate from strikes midpoint
                all_strikes = short_strikes + long_strikes
                spot = sum(all_strikes) / len(all_strikes) if all_strikes else 100.0

            spec = OptionsTradeSpec(
                strategy_type=strategy_type, ticker=ticker,
                expiry_date=expiry_date or '2026-12-31',
                legs=legs, net_debit=net_debit,
                max_risk=max_risk, max_reward=max_reward,
                reason=f'orphan_recovery_{ticker}')

            pos = OptionsPosition(
                ticker=ticker, strategy_type=strategy_type, spec=spec,
                entry_time=_time.monotonic(), entry_cost=net_debit,
                max_risk=max_risk, max_reward=max_reward,
                expiry_date=expiry_date or '2026-12-31',
                current_value=abs(net_debit), last_check_time=0)

            entry_iv = 0.25
            try:
                entry_iv = self._engine._estimate_iv(ticker, spot) or 0.25
            except Exception:
                pass
            if entry_iv < 0.05:
                entry_iv = 0.20

            lifecycle = OptionsPositionLifecycle(
                ticker=ticker, strategy_type=strategy_type,
                entry_cost=net_debit, max_risk=max_risk, max_reward=max_reward,
                entry_iv=entry_iv, entry_delta=0.0, entry_underlying=spot,
                short_strikes=tuple(short_strikes), long_strikes=tuple(long_strikes),
                atr=spot * 0.01, dte_at_entry=pos.dte)

            with self._engine._positions_lock:
                self._engine._positions[ticker] = pos
                self._engine._lifecycles[ticker] = lifecycle

            self._engine._risk.acquire(ticker, cost=net_debit, max_risk=max_risk)

            log.info(
                "[options] Orphan %s RECOVERED | strategy=%s | legs=%d | "
                "cost=$%.2f | max_risk=$%.2f | strikes_short=%s | dte=%d",
                ticker, strategy_type, len(legs), net_debit,
                max_risk, short_strikes, pos.dte)

        except Exception as exc:
            log.error("[options] Orphan %s recovery FAILED: %s — "
                      "requires manual review", ticker, exc)

    def _fetch_broker_legs(self, ticker: str) -> list:
        """Fetch all option contract positions for a ticker from Alpaca."""
        try:
            from alpaca.trading.client import TradingClient
            key = os.getenv('APCA_OPTIONS_KEY', '')
            secret = os.getenv('APCA_OPTIONS_SECRET', '')
            if not key or not secret:
                return []
            client = TradingClient(key, secret, paper=True)
            all_positions = client.get_all_positions()
            # Filter to legs matching this underlying ticker
            result = []
            for p in all_positions:
                sym = str(p.symbol)
                for i, ch in enumerate(sym):
                    if ch.isdigit():
                        underlying = sym[:i]
                        break
                else:
                    underlying = sym
                if underlying == ticker:
                    result.append(p)
            return result
        except Exception as exc:
            log.warning("[options] Failed to fetch broker legs for %s: %s", ticker, exc)
            return []

    @staticmethod
    def _infer_strategy(legs, short_strikes, long_strikes) -> str:
        """Infer options strategy type from leg structure.

        Uses leg count, sides (buy/sell), and option types (call/put from OCC)
        to classify the spread.
        """
        n_legs = len(legs)
        n_short = len(short_strikes)
        n_long = len(long_strikes)

        # Parse call/put from OCC symbols
        calls = [l for l in legs if len(l.symbol) > 10 and l.symbol[-9] == 'C']
        puts = [l for l in legs if len(l.symbol) > 10 and l.symbol[-9] == 'P']
        short_calls = [l for l in calls if l.side == 'sell']
        long_calls = [l for l in calls if l.side == 'buy']
        short_puts = [l for l in puts if l.side == 'sell']
        long_puts = [l for l in puts if l.side == 'buy']

        # 4 legs: iron condor or iron butterfly
        if n_legs == 4 and len(short_calls) == 1 and len(long_calls) == 1 \
                and len(short_puts) == 1 and len(long_puts) == 1:
            return 'iron_condor'

        # 2 call legs
        if n_legs == 2 and len(calls) == 2:
            if long_calls and short_calls:
                long_strike = long_strikes[0] if long_strikes else 0
                short_strike = short_strikes[0] if short_strikes else 0
                return 'bull_call_spread' if long_strike < short_strike else 'bear_call_spread'

        # 2 put legs
        if n_legs == 2 and len(puts) == 2:
            if long_puts and short_puts:
                long_strike = long_strikes[0] if long_strikes else 0
                short_strike = short_strikes[0] if short_strikes else 0
                return 'bear_put_spread' if long_strike > short_strike else 'bull_put_spread'

        # 1 call + 1 put (both long)
        if n_legs == 2 and len(long_calls) == 1 and len(long_puts) == 1:
            if long_strikes and len(set(long_strikes)) == 1:
                return 'long_straddle'
            return 'long_strangle'

        # Single long call
        if n_legs == 1 and len(long_calls) == 1:
            return 'long_call'

        # Single long put
        if n_legs == 1 and len(long_puts) == 1:
            return 'long_put'

        # 3 legs: butterfly
        if n_legs == 3:
            return 'butterfly_spread'

        # Fallback
        if n_short > n_long:
            return 'iron_condor'  # assume credit spread
        return 'long_call'  # assume debit

    def remove_position(self, ticker: str) -> None:
        with self._engine._positions_lock:
            if ticker in self._engine._positions:
                del self._engine._positions[ticker]
            # Clean up lifecycle too (prevents lingering state)
            self._engine._lifecycles.pop(ticker, None)
        self._engine._risk.release(ticker)
