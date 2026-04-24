"""
Test isolation — prevents tests from polluting production state files.

This conftest.py applies AUTOMATICALLY to every test in the test/ directory.
No test author action required — isolation is enforced by default.

What it does:
    1. Redirects all config.py state paths to a temp directory
    2. Resets the OrderWAL singleton to use the temp directory
    3. Creates necessary subdirectories (data/)
    4. Cleans up automatically after each test

What it prevents:
    - CRASH1/CRASH2 entries in production bot_state.json
    - Test kill_switch files (test_kill_switch.json) in data/
    - Test entries in position_broker_map.json
    - WAL files from test runs accumulating in data/

Override: If a specific test NEEDS production state (e.g., integration test),
use @pytest.mark.no_isolate and it will skip this fixture.
"""
import os
import pytest


@pytest.fixture(autouse=True)
def isolate_state(tmp_path, monkeypatch, request):
    """Redirect all state file paths to a temporary directory.

    autouse=True means this runs for EVERY test automatically.
    Tests write to /tmp/pytest-xxx/ — production files untouched.
    """
    # Allow specific tests to opt out
    if request.node.get_closest_marker('no_isolate'):
        yield
        return

    # Create temp data directory
    tmp_data = tmp_path / 'data'
    tmp_data.mkdir()

    # Patch config.py constants
    monkeypatch.setattr('config.STATE_DIR', str(tmp_path))
    monkeypatch.setattr('config.DATA_DIR', str(tmp_data))
    monkeypatch.setattr('config.BOT_STATE_PATH', str(tmp_path / 'bot_state.json'))
    monkeypatch.setattr('config.FILL_LEDGER_PATH', str(tmp_data / 'fill_ledger.json'))
    monkeypatch.setattr('config.OPTIONS_STATE_PATH', str(tmp_data / 'options_state.json'))
    monkeypatch.setattr('config.BROKER_MAP_PATH', str(tmp_data / 'position_broker_map.json'))
    monkeypatch.setattr('config.SUPERVISOR_STATUS_PATH', str(tmp_data / 'supervisor_status.json'))
    monkeypatch.setattr('config.LIVE_CACHE_PATH', str(tmp_data / 'live_cache.json'))

    # Patch environment variable (for modules that read STATE_DIR from env)
    monkeypatch.setenv('STATE_DIR', str(tmp_path))

    # Reset OrderWAL singleton to use temp directory
    try:
        import monitor.order_wal as wal_module
        # Save original state
        orig_wal_dir = wal_module.WAL_DIR
        orig_fd = wal_module.wal._fd
        # Redirect WAL to temp
        wal_module.WAL_DIR = str(tmp_data)
        wal_module.wal._wal_dir = str(tmp_data)
        wal_module.wal._order_states.clear()
        wal_module.wal._seq = 0
        wal_module.wal._current_date = ''
        if orig_fd:
            try:
                orig_fd.close()
            except Exception:
                pass
        wal_module.wal._fd = None
    except ImportError:
        orig_wal_dir = None

    yield tmp_path

    # Restore WAL (monkeypatch auto-restores config attrs)
    if orig_wal_dir is not None:
        try:
            wal_module.WAL_DIR = orig_wal_dir
            wal_module.wal._wal_dir = orig_wal_dir
            wal_module.wal._order_states.clear()
            wal_module.wal._seq = 0
            wal_module.wal._current_date = ''
            if wal_module.wal._fd:
                try:
                    wal_module.wal._fd.close()
                except Exception:
                    pass
            wal_module.wal._fd = None
        except Exception:
            pass


def pytest_configure(config):
    """Register the no_isolate marker."""
    config.addinivalue_line(
        'markers',
        'no_isolate: skip state file isolation (for integration tests that need production state)',
    )
