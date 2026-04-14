#!/usr/bin/env python3
"""
Close all SHORT positions created by the Side enum bug on 2026-04-13.

This script closes SHORT positions by submitting market BUY orders to cover them.

Usage:
    python close_short_positions.py
"""

import os
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

# Get API credentials from environment
API_KEY = os.getenv('ALPACA_API_KEY')
API_SECRET = os.getenv('ALPACA_SECRET_KEY')
BASE_URL = os.getenv('ALPACA_BASE_URL', 'https://paper-api.alpaca.markets')

if not API_KEY or not API_SECRET:
    print("ERROR: Set ALPACA_API_KEY and ALPACA_SECRET_KEY environment variables")
    exit(1)

# Initialize trading client
client = TradingClient(api_key=API_KEY, secret_key=API_SECRET, base_url=BASE_URL)

# SHORT positions to close (from 2026-04-13 bug)
SHORTS_TO_CLOSE = {
    'NOW': 11,
    'MRVL': 10,
    'INTU': 2,
    'QCOM': 7,
    'TEAM': 16,
    'HOOD': 14,
    'MU': 2,
    'SMCI': 39,
    'UPST': 37,
    'MSFT': 2,
    'ZM': 13,
    'AMAT': 2,
    'AVGO': 4,
    'ARM': 6,
    'DELL': 5,
}

def close_short_positions():
    """Close all SHORT positions by submitting market BUY orders."""

    print("Fetching all open positions...")
    positions = client.get_all_positions()

    # Find SHORT positions
    short_positions = {}
    for pos in positions:
        qty = float(pos.qty or 0)
        if qty < 0:  # SHORT position
            short_positions[pos.symbol] = abs(qty)

    if not short_positions:
        print("✓ No SHORT positions found in account")
        return

    print(f"\nFound {len(short_positions)} SHORT position(s):")
    for symbol, qty in sorted(short_positions.items()):
        print(f"  {symbol:6} {qty:6.0f} shares")

    print("\n" + "="*60)
    print("CLOSING SHORT POSITIONS...")
    print("="*60)

    closed = 0
    failed = 0

    for symbol, qty in sorted(short_positions.items()):
        try:
            # Buy to close SHORT position
            req = MarketOrderRequest(
                symbol=symbol,
                qty=int(qty),
                side=OrderSide.BUY,  # Buy to close SHORT
                time_in_force=TimeInForce.DAY,
            )
            order = client.submit_order(req)
            print(f"✓ {symbol:6} BUY  {int(qty):6} shares to close SHORT (order {order.id})")
            closed += 1
        except Exception as e:
            print(f"✗ {symbol:6} FAILED: {e}")
            failed += 1

    print("\n" + "="*60)
    print(f"Closed: {closed} | Failed: {failed}")
    print("="*60)

    if failed == 0:
        print("\n✓ All SHORT positions closed successfully!")
    else:
        print(f"\n⚠ {failed} position(s) failed to close. Check manually in Alpaca.")

if __name__ == '__main__':
    close_short_positions()
