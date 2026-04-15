"""
Crash Analyzer — reads tracebacks, diagnoses errors, suggests fixes.

Called by the watchdog when the monitor crashes. Returns a structured
fix recommendation that the watchdog can apply safely.

Handles common crash patterns:
  - TypeError: unexpected keyword argument
  - ImportError / ModuleNotFoundError
  - AttributeError: object has no attribute
  - UnboundLocalError
  - SyntaxError
  - ValueError from payload validation

For unknown errors: writes a crash_report.json for manual diagnosis.

STRICT GUARDRAILS:
  - Only suggests fixes in ALLOWED_FILES
  - Never touches DB schema, credentials, table names, or .env
  - Never changes method signatures or class __init__ parameters
  - Only fixes the specific line/pattern that crashed
"""
import json
import logging
import os
import re
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Optional

log = logging.getLogger(__name__)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Files the analyzer is allowed to suggest fixes for
ALLOWED_FILES = {
    'run_monitor.py',
    'pop_strategy_engine.py',
    'config.py',
    'monitor/monitor.py',
    'monitor/strategy_engine.py',
    'monitor/risk_engine.py',
    'monitor/signals.py',
    'monitor/brokers.py',
    'monitor/position_manager.py',
    'monitor/state_engine.py',
    'monitor/sector_map.py',
    'monitor/alerts.py',
    'monitor/event_bus.py',
    'monitor/events.py',
    'monitor/observability.py',
    'monitor/rvol.py',
    'options/engine.py',
    'options/chain.py',
    'options/selector.py',
    'options/risk.py',
    'options/iv_tracker.py',
    'options/portfolio_greeks.py',
    'pro_setups/engine.py',
    'pro_setups/risk/risk_adapter.py',
    'pro_setups/classifiers/strategy_classifier.py',
    'pop_screener/screener.py',
    'pop_screener/features.py',
    'pop_screener/classifier.py',
    'pop_screener/sentiment_baseline.py',
    'pop_screener/stocktwits_social.py',
    'pop_screener/benzinga_news.py',
    'pop_screener/ml_classifier.py',
    'pop_screener/unusual_options_flow.py',
    'pop_screener/strategy_router.py',
    'pop_screener/models.py',
    'analytics/risk_metrics.py',
    'analytics/attribution.py',
    'analytics/compliance.py',
    'dashboards/trading_dashboard.py',
    'db/event_sourcing_subscriber.py',
    'db/writer.py',
}

# Files that must NEVER be modified
FORBIDDEN_FILES = {
    '.env', '.env.local',
    'db/schema.sql', 'db/migrations/',
    'db/connection.py',  # connection strings / credentials
}

# Patterns that must never appear in a fix
FORBIDDEN_PATTERNS = [
    r'DATABASE_URL\s*=',
    r'APCA_API.*=\s*["\']',
    r'password\s*=',
    r'secret\s*=\s*["\']',
    r'CREATE\s+TABLE',
    r'ALTER\s+TABLE',
    r'DROP\s+TABLE',
]


@dataclass
class CrashDiagnosis:
    """Structured output from crash analysis."""
    error_type: str           # e.g. 'TypeError', 'AttributeError'
    error_message: str        # the actual error text
    file_path: str            # relative path to the crashing file
    line_number: int          # line that crashed
    function_name: str        # function/method that crashed
    root_cause: str           # human-readable diagnosis
    fix_applicable: bool      # can the analyzer fix this?
    fix_file: str = ''        # file to modify (relative path)
    fix_old: str = ''         # string to replace
    fix_new: str = ''         # replacement string
    fix_description: str = '' # what the fix does
    confidence: float = 0.0   # 0.0-1.0 confidence in the fix
    attempt: int = 0          # which retry attempt this is
    timestamp: str = ''       # when the crash was analyzed
    raw_traceback: str = ''   # full traceback text

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)


class CrashAnalyzer:
    """
    Analyzes monitor crash tracebacks and suggests fixes.

    Usage:
        analyzer = CrashAnalyzer()
        diagnosis = analyzer.analyze(traceback_text)
        if diagnosis.fix_applicable:
            # apply the fix
        else:
            # write crash report for manual review
    """

    def __init__(self):
        self._fix_count = 0
        self._max_fixes_per_session = 5  # safety: stop after 5 fixes

    def analyze(self, traceback_text: str, attempt: int = 0) -> CrashDiagnosis:
        """Parse a traceback and return a diagnosis with optional fix."""
        now = datetime.now().isoformat()

        # Parse traceback structure
        error_type, error_message = self._parse_error_line(traceback_text)
        file_path, line_number, function_name = self._parse_crash_location(traceback_text)

        diagnosis = CrashDiagnosis(
            error_type=error_type,
            error_message=error_message,
            file_path=file_path,
            line_number=line_number,
            function_name=function_name,
            root_cause='',
            fix_applicable=False,
            attempt=attempt,
            timestamp=now,
            raw_traceback=traceback_text,
        )

        # Safety: too many fixes in one session
        if self._fix_count >= self._max_fixes_per_session:
            diagnosis.root_cause = (
                f"Max fixes per session reached ({self._max_fixes_per_session}). "
                "Manual review required."
            )
            return diagnosis

        # Check if file is allowed
        if not self._is_allowed_file(file_path):
            diagnosis.root_cause = f"File {file_path} is not in allowed fix list"
            return diagnosis

        # Try pattern-based fixes
        for fixer in [
            self._fix_unexpected_kwarg,
            self._fix_attribute_error,
            self._fix_import_error,
            self._fix_unbound_local,
            self._fix_validation_error,
            self._fix_type_error_args,
        ]:
            result = fixer(diagnosis, traceback_text)
            if result and result.fix_applicable:
                # Validate the fix doesn't contain forbidden patterns
                if self._fix_is_safe(result):
                    self._fix_count += 1
                    return result
                else:
                    result.fix_applicable = False
                    result.root_cause += " [FIX BLOCKED: contains forbidden pattern]"
                    return result

        # No pattern matched — unknown error
        diagnosis.root_cause = (
            f"Unknown {error_type}: {error_message}. "
            f"Location: {file_path}:{line_number} in {function_name}. "
            "Manual review required."
        )
        return diagnosis

    def write_crash_report(self, diagnosis: CrashDiagnosis, output_dir: str = None):
        """Write a crash report JSON for manual diagnosis."""
        if output_dir is None:
            output_dir = os.path.join(PROJECT_ROOT, 'logs')
        os.makedirs(output_dir, exist_ok=True)
        report_path = os.path.join(
            output_dir,
            f"crash_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        )
        with open(report_path, 'w') as f:
            f.write(diagnosis.to_json())
        log.info("[CrashAnalyzer] Report written: %s", report_path)
        return report_path

    # ── Traceback parsing ────────────────────────────────────────────────

    def _parse_error_line(self, tb_text: str) -> tuple:
        """Extract error type and message from last line of traceback."""
        lines = tb_text.strip().split('\n')
        for line in reversed(lines):
            line = line.strip()
            if ':' in line and not line.startswith('File'):
                parts = line.split(':', 1)
                return parts[0].strip(), parts[1].strip() if len(parts) > 1 else ''
        return 'Unknown', tb_text[-200:]

    def _parse_crash_location(self, tb_text: str) -> tuple:
        """Extract file path, line number, and function from traceback."""
        # Match: File "path/to/file.py", line 104, in __init__
        matches = re.findall(
            r'File "([^"]+)", line (\d+), in (\w+)',
            tb_text
        )
        if matches:
            # Last match is the innermost frame (actual crash site)
            file_path, line_num, func = matches[-1]
            # Make relative to project root
            if PROJECT_ROOT in file_path:
                file_path = file_path.replace(PROJECT_ROOT + '/', '')
            return file_path, int(line_num), func
        return '', 0, ''

    def _is_allowed_file(self, file_path: str) -> bool:
        """Check if file is in the allowed modification list."""
        rel = file_path.replace(PROJECT_ROOT + '/', '')
        return rel in ALLOWED_FILES

    def _fix_is_safe(self, diagnosis: CrashDiagnosis) -> bool:
        """Check fix doesn't contain forbidden patterns."""
        for pattern in FORBIDDEN_PATTERNS:
            if re.search(pattern, diagnosis.fix_new, re.IGNORECASE):
                return False
        return True

    def _read_file_lines(self, file_path: str, center_line: int, context: int = 5) -> list:
        """Read lines around the crash site."""
        abs_path = os.path.join(PROJECT_ROOT, file_path) if not os.path.isabs(file_path) else file_path
        try:
            with open(abs_path) as f:
                lines = f.readlines()
            start = max(0, center_line - context - 1)
            end = min(len(lines), center_line + context)
            return lines[start:end]
        except Exception:
            return []

    # ── Pattern-based fixers ─────────────────────────────────────────────

    def _fix_unexpected_kwarg(self, diag: CrashDiagnosis, tb: str) -> Optional[CrashDiagnosis]:
        """Fix: TypeError: X() got an unexpected keyword argument 'Y'"""
        m = re.search(
            r"got an unexpected keyword argument .(\w+)",
            tb
        )
        if not m:
            return None

        bad_kwarg = m.group(1)

        # Read the crash site to find the call
        lines = self._read_file_lines(diag.file_path, diag.line_number, context=10)
        if not lines:
            return None

        # Find the line with the bad kwarg
        for line in lines:
            if bad_kwarg in line and '=' in line:
                stripped = line.strip()
                diag.fix_applicable = True
                diag.fix_file = diag.file_path
                diag.fix_old = stripped
                diag.fix_new = f"# REMOVED: {stripped}  # caused TypeError: unexpected kwarg"
                diag.fix_description = (
                    f"Comment out '{bad_kwarg}=' argument — "
                    f"this version of the library doesn't accept it"
                )
                diag.root_cause = (
                    f"Constructor doesn't accept '{bad_kwarg}' parameter. "
                    f"Library API may have changed."
                )
                diag.confidence = 0.85
                return diag
        return None

    def _fix_attribute_error(self, diag: CrashDiagnosis, tb: str) -> Optional[CrashDiagnosis]:
        """Fix: AttributeError: 'X' object has no attribute 'Y'"""
        m = re.search(
            r"AttributeError: .(\w+). object has no attribute .(\w+)",
            tb
        )
        if not m:
            return None

        obj_type = m.group(1)
        missing_attr = m.group(2)

        # Read crash site
        lines = self._read_file_lines(diag.file_path, diag.line_number, context=5)
        if not lines:
            return None

        # Suggest using getattr with default
        for line in lines:
            if f'.{missing_attr}' in line:
                stripped = line.strip()
                # Replace obj.attr with getattr(obj, 'attr', default)
                # Find the object reference
                attr_match = re.search(rf'(\w+)\.{missing_attr}\b', stripped)
                if attr_match:
                    obj_ref = attr_match.group(1)
                    old_access = f"{obj_ref}.{missing_attr}"
                    new_access = f"getattr({obj_ref}, '{missing_attr}', None)"

                    diag.fix_applicable = True
                    diag.fix_file = diag.file_path
                    diag.fix_old = stripped
                    diag.fix_new = stripped.replace(old_access, new_access)
                    diag.fix_description = (
                        f"Replace .{missing_attr} access with getattr() fallback — "
                        f"'{obj_type}' doesn't have this attribute"
                    )
                    diag.root_cause = (
                        f"'{obj_type}' object doesn't have attribute '{missing_attr}'. "
                        f"Attribute may have been renamed or the object type changed."
                    )
                    diag.confidence = 0.70
                    return diag
        return None

    def _fix_import_error(self, diag: CrashDiagnosis, tb: str) -> Optional[CrashDiagnosis]:
        """Fix: ImportError / ModuleNotFoundError"""
        m = re.search(
            r"(?:ImportError|ModuleNotFoundError): (?:No module named |cannot import name ).?(\w[\w.]*)",
            tb
        )
        if not m:
            return None

        missing = m.group(1)

        # Read the import line
        lines = self._read_file_lines(diag.file_path, diag.line_number, context=3)
        for line in lines:
            if 'import' in line and missing in line:
                stripped = line.strip()
                diag.fix_applicable = True
                diag.fix_file = diag.file_path
                diag.fix_old = stripped
                # Wrap in try/except
                indent = len(line) - len(line.lstrip())
                pad = ' ' * indent
                diag.fix_new = (
                    f"try:\n"
                    f"{pad}    {stripped}\n"
                    f"{pad}except ImportError:\n"
                    f"{pad}    pass  # auto-fix: module '{missing}' unavailable"
                )
                diag.fix_description = (
                    f"Wrap import of '{missing}' in try/except — "
                    f"module may not be installed or path changed"
                )
                diag.root_cause = f"Module '{missing}' not found. May need pip install or path fix."
                diag.confidence = 0.60
                return diag
        return None

    def _fix_unbound_local(self, diag: CrashDiagnosis, tb: str) -> Optional[CrashDiagnosis]:
        """Fix: UnboundLocalError: cannot access local variable 'X'"""
        m = re.search(
            r"UnboundLocalError: cannot access local variable .(\w+)",
            tb
        )
        if not m:
            return None

        var_name = m.group(1)
        diag.root_cause = (
            f"Variable '{var_name}' used before assignment. "
            f"Likely an import that failed silently or a conditional branch "
            f"that didn't assign the variable."
        )
        diag.fix_description = (
            f"UnboundLocalError for '{var_name}' — needs manual review "
            f"to determine correct assignment/import path"
        )
        # UnboundLocal is too context-dependent for auto-fix
        diag.confidence = 0.0
        return diag

    def _fix_validation_error(self, diag: CrashDiagnosis, tb: str) -> Optional[CrashDiagnosis]:
        """Fix: ValueError from payload __post_init__ validation."""
        if 'ValueError' not in tb or '__post_init__' not in tb:
            return None

        m = re.search(r"ValueError: (.+)", tb)
        if not m:
            return None

        diag.root_cause = (
            f"Payload validation failed: {m.group(1)}. "
            f"A value passed to a dataclass payload is out of expected range."
        )
        # Validation errors need manual review — the fix is in the caller, not the validator
        diag.confidence = 0.0
        return diag

    def _fix_type_error_args(self, diag: CrashDiagnosis, tb: str) -> Optional[CrashDiagnosis]:
        """Fix: TypeError: X() takes N positional arguments but M were given"""
        m = re.search(
            r"TypeError: (\w+)\(\) takes (\d+) positional argument.* but (\d+) (?:was|were) given",
            tb
        )
        if not m:
            return None

        func_name = m.group(1)
        expected = int(m.group(2))
        given = int(m.group(3))

        diag.root_cause = (
            f"{func_name}() expects {expected} args but got {given}. "
            f"Function signature may have changed."
        )
        diag.fix_description = (
            f"Argument count mismatch for {func_name}() — "
            f"needs manual review to determine which args to add/remove"
        )
        diag.confidence = 0.0
        return diag
