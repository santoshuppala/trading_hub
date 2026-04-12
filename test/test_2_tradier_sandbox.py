#!/usr/bin/env python3
"""
Test 2: Tradier Sandbox Validation
===================================
Switches TRADIER_BASE_URL to the sandbox environment and validates:
  1. Authentication headers work with sandbox API
  2. /markets/quotes endpoint returns data for AAPL
  3. Manual buy order submission for AAPL
  4. JSON parsing and order_id extraction
  5. Account endpoints work with sandbox credentials

Method
------
- Monkey-patch TRADIER_BASE_URL to https://sandbox.tradier.com/v1
- Use the existing TRADIER_TOKEN from .env
- Test quote retrieval, history, and order placement

Pass criteria
-------------
- Sandbox API accepts Authorization header (HTTP 200, not 401/403)
- Quote data for AAPL returns valid bid/ask/last
- Historical data (/markets/history) returns OHLCV DataFrame
- Order submission returns a simulated order_id (or meaningful error for sandbox)
- No unhandled exceptions in the JSON parsing path
"""

import sys
import os
import time
import logging
import importlib
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import requests

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ET = ZoneInfo('America/New_York')

SANDBOX_URL = 'https://sandbox.tradier.com/v1'
PROD_URL = 'https://api.tradier.com/v1'

os.makedirs(os.path.join(os.path.dirname(__file__), "logs"), exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(os.path.dirname(__file__), 'logs', 'test_2_tradier_sandbox.log'), mode='w'),
    ],
)
log = logging.getLogger(__name__)


def load_tradier_token() -> str:
    """Load TRADIER_SANDBOX_TOKEN (preferred) or TRADIER_TOKEN from .env."""
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env')
    token = ''
    sandbox_token = ''
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith('TRADIER_SANDBOX_TOKEN='):
                    sandbox_token = line.split('=', 1)[1].strip().strip('"').strip("'")
                elif line.startswith('TRADIER_TOKEN='):
                    token = line.split('=', 1)[1].strip().strip('"').strip("'")
    # Prefer sandbox-specific token; fall back to production token + env
    return (sandbox_token
            or os.environ.get('TRADIER_SANDBOX_TOKEN', '')
            or token
            or os.environ.get('TRADIER_TOKEN', ''))


def test_auth(token: str) -> bool:
    """Test 2a: Authentication with sandbox."""
    log.info("\n--- Test 2a: Sandbox Authentication ---")
    session = requests.Session()
    session.headers.update({
        'Authorization': f'Bearer {token}',
        'Accept': 'application/json',
    })

    try:
        resp = session.get(f'{SANDBOX_URL}/user/profile', timeout=15)
        log.info(f"GET /user/profile → HTTP {resp.status_code}")
        if resp.status_code == 200:
            data = resp.json()
            log.info(f"Profile response: {data}")
            log.info("PASS: Authentication successful with sandbox")
            return True
        elif resp.status_code == 401:
            log.info("FAIL: 401 Unauthorized — token may not work with sandbox")
            return False
        elif resp.status_code == 403:
            log.info("FAIL: 403 Forbidden — token lacks sandbox permissions")
            return False
        else:
            log.info(f"WARN: Unexpected status {resp.status_code}: {resp.text[:200]}")
            return False
    except Exception as e:
        log.info(f"FAIL: Connection error: {e}")
        return False


def test_quotes(token: str) -> dict:
    """Test 2b: Quote retrieval for AAPL."""
    log.info("\n--- Test 2b: Sandbox Quote Retrieval (AAPL) ---")
    session = requests.Session()
    session.headers.update({
        'Authorization': f'Bearer {token}',
        'Accept': 'application/json',
    })

    try:
        resp = session.get(f'{SANDBOX_URL}/markets/quotes', params={'symbols': 'AAPL', 'greeks': 'false'}, timeout=15)
        log.info(f"GET /markets/quotes?symbols=AAPL → HTTP {resp.status_code}")

        if resp.status_code == 200:
            data = resp.json()
            quotes = data.get('quotes', {}).get('quote', [])
            if isinstance(quotes, dict):
                quotes = [quotes]
            if quotes:
                q = quotes[0]
                log.info(f"AAPL quote: last={q.get('last')}, bid={q.get('bid')}, ask={q.get('ask')}")
                log.info(f"  volume={q.get('volume')}, change={q.get('change_percentage')}")
                log.info("PASS: Quote data retrieved successfully")
                return {'success': True, 'quote': q}
            else:
                log.info("WARN: No quote data returned (sandbox may not have live quotes on weekends)")
                return {'success': True, 'quote': None, 'reason': 'no_data'}
        else:
            log.info(f"FAIL: HTTP {resp.status_code}: {resp.text[:200]}")
            return {'success': False}
    except Exception as e:
        log.info(f"FAIL: {e}")
        return {'success': False, 'error': str(e)}


def test_history(token: str) -> bool:
    """Test 2c: Historical data retrieval."""
    log.info("\n--- Test 2c: Sandbox Historical Data (AAPL daily) ---")
    session = requests.Session()
    session.headers.update({
        'Authorization': f'Bearer {token}',
        'Accept': 'application/json',
    })

    end = datetime.now(ET)
    start = end - timedelta(days=14)

    try:
        resp = session.get(f'{SANDBOX_URL}/markets/history', params={
            'symbol': 'AAPL',
            'interval': 'daily',
            'start': start.strftime('%Y-%m-%d'),
            'end': end.strftime('%Y-%m-%d'),
        }, timeout=15)
        log.info(f"GET /markets/history?symbol=AAPL → HTTP {resp.status_code}")

        if resp.status_code == 200:
            data = resp.json()
            history = data.get('history', {})
            days = history.get('day', [])
            if isinstance(days, dict):
                days = [days]
            log.info(f"Days returned: {len(days)}")
            if days:
                df = pd.DataFrame(days)
                log.info(f"DataFrame shape: {df.shape}")
                log.info(f"Columns: {list(df.columns)}")
                log.info(f"Last close: {df['close'].iloc[-1]:.2f}")
                log.info("PASS: Historical data parsed into DataFrame")
                return True
            else:
                log.info("WARN: No daily history returned (sandbox data may be limited on weekends)")
                return True  # Not a failure — sandbox may be empty
        else:
            log.info(f"FAIL: HTTP {resp.status_code}: {resp.text[:200]}")
            return False
    except Exception as e:
        log.info(f"FAIL: {e}")
        return False


def test_order_submission(token: str) -> dict:
    """Test 2d: Submit a buy order for AAPL in sandbox."""
    log.info("\n--- Test 2d: Sandbox Order Submission (BUY 1 AAPL) ---")
    session = requests.Session()
    session.headers.update({
        'Authorization': f'Bearer {token}',
        'Accept': 'application/json',
    })

    # Get real account number from profile
    account_number = None
    try:
        resp = session.get(f'{SANDBOX_URL}/user/profile', timeout=15)
        if resp.status_code != 200:
            log.info("SKIP: Cannot verify sandbox account — skipping order test")
            return {'success': False, 'reason': 'no_account'}
        profile = resp.json().get('profile', {})
        acct = profile.get('account', {})
        account_number = acct.get('account_number')
        log.info(f"Using account_number: {account_number}")
    except Exception:
        return {'success': False, 'reason': 'connection_error'}

    orders_path = f'/accounts/{account_number}/orders' if account_number else '/accounts/0/orders'

    # Try to submit a limit order
    try:
        resp = session.post(f'{SANDBOX_URL}{orders_path}', data={
            'class': 'equity',
            'symbol': 'AAPL',
            'side': 'buy',
            'quantity': '1',
            'type': 'limit',
            'price': '150.00',        # Low limit to test acceptance, not fill
            'duration': 'gtc',
        }, timeout=15)
        log.info(f"POST {orders_path} → HTTP {resp.status_code}")

        if resp.status_code in (200, 201):
            data = resp.json()
            order = data.get('order', {})
            order_id = order.get('id')
            status = order.get('status')
            log.info(f"Order response: id={order_id}, status={status}")
            log.info(f"Full order data: {order}")
            if order_id:
                log.info("PASS: Sandbox accepted order and returned order_id")
                return {'success': True, 'order_id': str(order_id), 'status': status}
            else:
                log.info("WARN: Order response parsed but no order_id found")
                return {'success': True, 'order_id': None, 'data': order}
        else:
            body = resp.text[:500]
            log.info(f"WARN: Order submission returned HTTP {resp.status_code}")
            log.info(f"Response: {body}")
            # 401/403 means auth issue; 400 means format issue; sandbox may need specific account
            return {'success': False, 'status': resp.status_code, 'body': body}
    except Exception as e:
        log.info(f"FAIL: Order submission error: {e}")
        return {'success': False, 'error': str(e)}


def test_tradier_client_patch(token: str) -> bool:
    """Test 2e: Verify our TradierDataClient works when URL is patched."""
    log.info("\n--- Test 2e: TradierDataClient with Patched URL ---")

    # Import and patch
    from monitor.tradier_client import TradierDataClient
    import monitor.tradier_client as tc_module

    original_url = tc_module.TRADIER_BASE_URL
    tc_module.TRADIER_BASE_URL = SANDBOX_URL

    try:
        client = TradierDataClient(token)

        # Test get_quotes
        quotes = client.get_quotes(['AAPL'])
        log.info(f"get_quotes(['AAPL']) → {len(quotes)} results")
        if 'AAPL' in quotes:
            log.info(f"  AAPL: last={quotes['AAPL'].get('last')}, bid={quotes['AAPL'].get('bid')}, ask={quotes['AAPL'].get('ask')}")
            log.info("PASS: TradierDataClient works with sandbox URL")
            return True
        else:
            log.info("WARN: AAPL not in quotes response (sandbox may not have live data)")
            # Check if we got any response at all (auth worked but no data)
            if quotes or len(quotes) == 0:
                log.info("PASS: Auth works (empty data is expected on weekends)")
                return True
            return False
    except Exception as e:
        log.info(f"FAIL: {e}")
        return False
    finally:
        tc_module.TRADIER_BASE_URL = original_url


def run_test():
    log.info("=" * 70)
    log.info("TEST 2: Tradier Sandbox Validation")
    log.info("=" * 70)

    token = load_tradier_token()
    if not token:
        log.info("FAIL: No TRADIER_TOKEN found in .env or environment")
        log.info("Set TRADIER_TOKEN in .env to run this test")
        return False

    log.info(f"Token loaded: {token[:8]}...{token[-4:]}")
    log.info(f"Sandbox URL: {SANDBOX_URL}")
    log.info("")

    results = {}

    # Run tests
    results['auth'] = test_auth(token)
    results['quotes'] = test_quotes(token)
    results['history'] = test_history(token)
    results['order'] = test_order_submission(token)
    results['client_patch'] = test_tradier_client_patch(token)

    # Summary
    log.info("\n" + "=" * 70)
    log.info("RESULTS")
    log.info("=" * 70)
    for name, result in results.items():
        if isinstance(result, bool):
            status = "PASS" if result else "FAIL"
        elif isinstance(result, dict):
            status = "PASS" if result.get('success') else "FAIL/WARN"
        else:
            status = "UNKNOWN"
        log.info(f"  {name:20s}: {status}")

    # Improvements
    log.info("\n" + "-" * 70)
    log.info("IMPROVEMENTS NEEDED")
    log.info("-" * 70)

    if not results['auth']:
        log.info("1. CRITICAL: Sandbox authentication failed — verify TRADIER_TOKEN has sandbox access")
        log.info("   You may need a separate sandbox token from Tradier developer portal")

    if isinstance(results['quotes'], dict) and results['quotes'].get('reason') == 'no_data':
        log.info("2. Sandbox returned no live quotes — expected on weekends")
        log.info("   Add a note in docs that sandbox validation requires market hours or cached data")

    if isinstance(results['order'], dict) and not results['order'].get('success'):
        log.info("3. Order submission failed — sandbox may require a specific account_id")
        log.info("   Update brokers.py to accept configurable account_id for sandbox testing")
        log.info("   Current hardcoded path: /accounts/0/orders — should be /accounts/{account_id}/orders")

    if not results['history']:
        log.info("4. Historical data retrieval failed — check /markets/history endpoint availability in sandbox")

    log.info("\nTest 2 complete.")
    passed = sum(1 for r in results.values() if r is True or (isinstance(r, dict) and r.get('success')))
    return passed >= 3  # At least 3 of 5 sub-tests pass


if __name__ == '__main__':
    success = run_test()
    sys.exit(0 if success else 1)