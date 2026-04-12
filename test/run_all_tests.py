#!/usr/bin/env python3
"""
Trading Hub — Weekend Sandbox Validation Runner
================================================
Runs all 5 weekend tests and writes a consolidated report to
test/logs/test_report.md.

Usage
-----
    cd trading_hub
    python test/run_all_tests.py

Each test writes its own detailed log to test/logs/<name>.log.
The runner captures exit codes + wall-clock times and writes a summary.
"""
import sys, os, time, subprocess, textwrap
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo('America/New_York')

HERE     = os.path.dirname(os.path.abspath(__file__))
PROJ_ROOT = os.path.dirname(HERE)
LOGS_DIR  = os.path.join(HERE, 'logs')
os.makedirs(LOGS_DIR, exist_ok=True)

TESTS = [
    {
        'id':    'T1',
        'name':  'Synthetic Feed Playback',
        'file':  'test_1_synthetic_feed.py',
        'desc':  '200 tickers × 100 bar steps via EventBus ASYNC at 10ms intervals',
        'needs': [],
    },
    {
        'id':    'T2',
        'name':  'Tradier Sandbox Validation',
        'file':  'test_2_tradier_sandbox.py',
        'desc':  'Auth + quotes + history + order submission via sandbox.tradier.com',
        'needs': ['TRADIER_TOKEN'],
    },
    {
        'id':    'T3',
        'name':  'Redpanda Consistency Check',
        'file':  'test_3_redpanda_consistency.py',
        'desc':  'Serialisation round-trip + (if Redpanda running) replay ordering',
        'needs': [],
    },
    {
        'id':    'T4',
        'name':  'Market Open Latency Simulation',
        'file':  'test_4_market_open_latency.py',
        'desc':  '200 BAR events injected simultaneously; P95 latency target < 50ms',
        'needs': [],
    },
    {
        'id':    'T5',
        'name':  'Network Warm-up Pre-fetch',
        'file':  'test_5_network_warmup.py',
        'desc':  'Parallel RVOL baseline fetch for 200 tickers; target < 60s',
        'needs': ['TRADIER_TOKEN'],
    },
    {
        'id':    'T6',
        'name':  'Full Pipeline Integration Stress',
        'file':  'test_6_pipeline_integration.py',
        'desc':  'BAR→SIGNAL→ORDER_REQ→FILL→POSITION chain; max-positions gate; EOD close; concurrent 10 tickers',
        'needs': [],
    },
    {
        'id':    'T7',
        'name':  'Risk Engine Boundary Conditions',
        'file':  'test_7_risk_boundaries.py',
        'desc':  'All 6 pre-trade checks at exact boundaries; partial sell qty=1; concurrent fills thread safety',
        'needs': [],
    },
    {
        'id':    'T8',
        'name':  'SignalAnalyzer Edge Cases',
        'file':  'test_8_signal_edge_cases.py',
        'desc':  'NaN/zero/insufficient bars; RVOL time gate; SignalPayload invariants; 1000-frame stress',
        'needs': [],
    },
    {
        'id':    'T9',
        'name':  'EventBus Advanced Behaviors',
        'file':  'test_9_eventbus_advanced.py',
        'desc':  'Circuit breaker; DLQ; TTL expiry; priority ordering; coalescing; retry; dedup; stream_seq',
        'needs': [],
    },
    {
        'id':    'T10',
        'name':  'State Persistence & Observability',
        'file':  'test_10_state_persistence.py',
        'desc':  'Round-trip save/load; corrupt JSON backup; concurrent writes; StateEngine; HeartbeatEmitter',
        'needs': [],
    },
]


def _load_dotenv():
    """Load .env into os.environ so tests that check env vars can find tokens."""
    env_path = os.path.join(PROJ_ROOT, '.env')
    if not os.path.exists(env_path):
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, _, val = line.partition('=')
                os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def _check_needs(needs: list) -> list:
    """Return list of unmet prerequisites."""
    missing = []
    for n in needs:
        if not os.environ.get(n):
            missing.append(n)
    return missing


def _run_one(test: dict) -> dict:
    """Run a single test script in a subprocess. Return result dict."""
    script = os.path.join(HERE, test['file'])
    missing = _check_needs(test['needs'])

    result = {
        'id':      test['id'],
        'name':    test['name'],
        'file':    test['file'],
        'desc':    test['desc'],
        'needs':   test['needs'],
        'missing': missing,
        'skipped': bool(missing),
        'passed':  False,
        'elapsed': 0.0,
        'stdout':  '',
        'stderr':  '',
        'rc':      None,
    }

    if missing:
        result['reason'] = f"SKIP — missing env: {', '.join(missing)}"
        print(f"\n  [{test['id']}] {test['name']} — SKIPPED (missing: {', '.join(missing)})")
        return result

    print(f"\n  [{test['id']}] {test['name']} …", flush=True)
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            [sys.executable, script],
            cwd=PROJ_ROOT,
            capture_output=True,
            text=True,
            timeout=600,   # 10-minute hard cap per test
        )
        result['elapsed'] = time.monotonic() - t0
        result['rc']      = proc.returncode
        result['stdout']  = proc.stdout
        result['stderr']  = proc.stderr
        result['passed']  = proc.returncode == 0

        # Save combined output to logs/
        out_path = os.path.join(LOGS_DIR, f"{test['file'].replace('.py', '')}_stdout.txt")
        with open(out_path, 'w') as f:
            f.write(f"=== STDOUT ===\n{proc.stdout}\n=== STDERR ===\n{proc.stderr}\n")

        status = "PASS" if result['passed'] else "FAIL"
        print(f"      {status} ({result['elapsed']:.1f}s)", flush=True)

    except subprocess.TimeoutExpired:
        result['elapsed'] = time.monotonic() - t0
        result['passed']  = False
        result['reason']  = "TIMEOUT (>600s)"
        print(f"      TIMEOUT ({result['elapsed']:.0f}s)", flush=True)
    except Exception as e:
        result['elapsed'] = time.monotonic() - t0
        result['passed']  = False
        result['reason']  = str(e)
        print(f"      ERROR: {e}", flush=True)

    return result


def _write_report(results: list, total_elapsed: float):
    """Write markdown test report."""
    now      = datetime.now(ET).strftime('%Y-%m-%d %H:%M:%S ET')
    passed   = sum(1 for r in results if r['passed'])
    skipped  = sum(1 for r in results if r['skipped'])
    failed   = len(results) - passed - skipped
    report   = os.path.join(LOGS_DIR, 'test_report.md')

    lines = [
        f"# Trading Hub — Weekend Validation Report",
        f"",
        f"**Run date:** {now}  ",
        f"**Python:** {sys.version.split()[0]}  ",
        f"**Total time:** {total_elapsed:.1f}s  ",
        f"**Results:** {passed} PASS / {failed} FAIL / {skipped} SKIP  ",
        f"",
        f"---",
        f"",
        f"## Summary",
        f"",
        f"| # | Test | Result | Time | Notes |",
        f"|---|------|--------|------|-------|",
    ]
    for r in results:
        if r['skipped']:
            badge = "⏭ SKIP"
            note  = r.get('reason', f"missing env: {', '.join(r['missing'])}")
        elif r['passed']:
            badge = "✅ PASS"
            note  = ""
        else:
            badge = "❌ FAIL"
            note  = r.get('reason', f"exit code {r['rc']}")
        elapsed_str = f"{r['elapsed']:.1f}s" if r['elapsed'] else "—"
        lines.append(f"| {r['id']} | {r['name']} | {badge} | {elapsed_str} | {note} |")

    lines += ["", "---", ""]

    for r in results:
        lines.append(f"## {r['id']}: {r['name']}")
        lines.append(f"")
        lines.append(f"**What it tests:** {r['desc']}  ")
        if r['needs']:
            lines.append(f"**Prerequisites:** {', '.join(r['needs'])}  ")
        lines.append(f"")

        if r['skipped']:
            lines.append(f"> **SKIPPED** — {r.get('reason', '')}  ")
            lines.append(f"> To run: set `{', '.join(r['missing'])}` in `.env`")
        else:
            status_hdr = "PASS" if r['passed'] else "FAIL"
            lines.append(f"**Status:** {status_hdr} ({r['elapsed']:.1f}s, exit={r['rc']})  ")
            lines.append(f"")

            # Combine stdout + stderr for extraction (tests log via logging → stderr)
            all_output = (r['stdout'] or '') + (r['stderr'] or '')

            # Extract VERDICT and IMPROVEMENTS lines
            verdict_lines = []
            improvement_lines = []
            in_verdict = in_improvement = False
            for line in all_output.splitlines():
                # Strip logging prefix "[INFO] " etc. before pattern matching
                bare = line.split('] ', 1)[-1] if '] ' in line else line
                if 'VERDICT' in bare and '-' * 10 in bare:
                    in_verdict = True
                    in_improvement = False
                    continue
                if 'IMPROVEMENTS NEEDED' in bare:
                    in_improvement = True
                    in_verdict = False
                    continue
                if 'Test ' in bare and 'complete' in bare:
                    in_verdict = in_improvement = False
                if in_verdict and bare.strip():
                    verdict_lines.append(bare.strip())
                if in_improvement and bare.strip():
                    improvement_lines.append(bare.strip())

            if verdict_lines:
                lines.append("**Verdict:**")
                lines.append("```")
                lines.extend(verdict_lines[:10])
                lines.append("```")
                lines.append("")

            if improvement_lines:
                lines.append("**Improvements needed:**")
                for il in improvement_lines[:6]:
                    lines.append(f"- {il}")
                lines.append("")

            # Key metrics lines
            key_metrics = [
                line.split('] ', 1)[-1].strip() if '] ' in line else line.strip()
                for line in all_output.splitlines()
                if any(kw in line for kw in [
                    'Events emitted', 'Emit throughput', 'Drain time',
                    'Tickers processed', 'system_pressure', 'Dropped events',
                    'Latency', 'p50', 'p95', 'p99', 'wall-clock',
                    'PASS', 'FAIL', 'WARN', 'auth', 'AAPL',
                ]) and line.strip()
            ]
            if key_metrics:
                lines.append("<details><summary>Key metrics from log</summary>")
                lines.append("")
                lines.append("```")
                lines.extend(key_metrics[:25])
                lines.append("```")
                lines.append("")
                lines.append("</details>")

        lines.append("")
        lines.append(f"**Log file:** `test/logs/{r['file'].replace('.py', '')}_stdout.txt`  ")
        lines.append("")
        lines.append("---")
        lines.append("")

    lines += [
        "## Readiness Assessment",
        "",
        "| Component | Status | Monday-Ready? |",
        "|-----------|--------|---------------|",
    ]
    component_map = {
        'T1': ('EventBus + ASYNC dispatcher (200 tickers)', True),
        'T2': ('Tradier API auth + order routing', False),
        'T3': ('Payload serialisation + Redpanda replay', True),
        'T4': ('StrategyEngine per-ticker latency', True),
        'T5': ('RVOL pre-fetch (200 tickers parallel)', False),
        'T6': ('Full pipeline BAR→SIGNAL→ORDER→FILL→POSITION', False),
        'T7': ('RiskEngine 6-check boundaries + thread safety', False),
        'T8': ('SignalAnalyzer edge cases + RVOL time gate', False),
        'T9': ('EventBus circuit-breaker / DLQ / TTL / coalescing', False),
        'T10': ('State persistence + StateEngine + HeartbeatEmitter', False),
    }
    for r in results:
        comp, needs_external = component_map.get(r['id'], (r['name'], False))
        if r['skipped']:
            status    = "⏭ Skipped (no credentials)"
            ready     = "⚠️ Verify manually"
        elif r['passed']:
            status    = "✅ All checks passed"
            ready     = "✅ Ready"
        else:
            status    = "❌ Checks failed"
            ready     = "❌ Fix before Monday"
        lines.append(f"| {comp} | {status} | {ready} |")

    # Build dynamic next-steps based on what failed
    next_steps = ["", "---", "", "## Next Steps", ""]
    step = 1

    t4 = next((r for r in results if r['id'] == 'T4'), None)
    if t4 and not t4['passed'] and not t4['skipped']:
        next_steps += [
            f"{step}. **[BLOCKER — fix before Monday]** T4 P95 latency > 50ms target.",
            "   - Raise BAR `n_workers` 4 → 8 in `_DEFAULT_ASYNC_CONFIG` (`monitor/event_bus.py`)",
            "   - Or pre-compute VWAP/RSI/ATR before injecting BAR events to eliminate pandas GIL contention",
            "",
        ]
        step += 1

    t2 = next((r for r in results if r['id'] == 'T2'), None)
    if t2 and not t2['passed'] and not t2['skipped']:
        next_steps += [
            f"{step}. **Fix Tradier sandbox token** — add `TRADIER_SANDBOX_TOKEN` to `.env`"
            " (separate key from developer.tradier.com).",
            "",
        ]
        step += 1

    next_steps += [
        f"{step}. **Start Redpanda** (`docker run -p 9092:9092 redpandadata/redpanda`) and",
        "   re-run Test 3 to verify full replay + crash recovery.",
        "",
    ]
    step += 1
    next_steps += [
        f"{step}. **Sunday evening**: run `python test/test_5_network_warmup.py` to pre-warm",
        "   the RVOL baseline cache so it's ready before 9:30 AM.",
        "",
    ]
    step += 1
    next_steps += [
        f"{step}. **Monday 9:25 AM**: start the monitor with `python run_monitor.py` and",
        "   watch `logs/monitor_<date>.log` for the first heartbeat at 9:30.",
        "",
        f"*Generated by run_all_tests.py at {now}*",
    ]
    lines += next_steps

    with open(report, 'w') as f:
        f.write('\n'.join(lines))
    return report


def main():
    _load_dotenv()

    print("=" * 70)
    print("Trading Hub — Weekend Sandbox Validation")
    print("=" * 70)
    print(f"Python {sys.version.split()[0]} | {datetime.now(ET).strftime('%Y-%m-%d %H:%M ET')}")
    print(f"Project root: {PROJ_ROOT}")
    print(f"Logs dir:     {LOGS_DIR}")
    print(f"\nRunning {len(TESTS)} test(s)…")

    results = []
    t_all   = time.monotonic()
    for test in TESTS:
        results.append(_run_one(test))
    total_elapsed = time.monotonic() - t_all

    passed  = sum(1 for r in results if r['passed'])
    failed  = sum(1 for r in results if not r['passed'] and not r['skipped'])
    skipped = sum(1 for r in results if r['skipped'])

    print("\n" + "=" * 70)
    print("FINAL SUMMARY")
    print("=" * 70)
    for r in results:
        if r['skipped']:
            tag = "SKIP"
        elif r['passed']:
            tag = "PASS"
        else:
            tag = "FAIL"
        elapsed_str = f"{r['elapsed']:.1f}s" if r['elapsed'] else "—"
        print(f"  [{r['id']}] {r['name']:<40s} {tag:<6} {elapsed_str}")

    print(f"\nTotal: {passed} PASS / {failed} FAIL / {skipped} SKIP  "
          f"({total_elapsed:.1f}s)")

    report = _write_report(results, total_elapsed)
    print(f"\nReport written to: {report}")

    return 0 if failed == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
