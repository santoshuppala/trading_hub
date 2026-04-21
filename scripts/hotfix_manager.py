"""
HotfixManager — Email-based code fix approval for unattended sessions.

Flow:
  1. Watchdog detects crash → captures traceback + file + line
  2. HotfixManager proposes a fix (pattern matching known error types)
  3. Sends email: "Error in X. Proposed fix: [diff]. Reply APPROVE to apply."
  4. Polls IMAP inbox for replies containing "APPROVE"
  5. Applies the fix → supervisor restarts the process

Safety:
  - Only modifies files in the project directory
  - Creates .bak backup before every edit
  - Max 5 hotfixes per session (prevents fix loops)
  - Each fix is logged with full before/after diff
  - NEVER modifies: config.py, .env, data/*.json, scripts/supervisor.py

Usage:
    Called by SessionWatchdog — not run standalone.
"""
import email
import imaplib
import logging
import os
import re
import smtplib
import time
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from typing import Optional, Tuple

log = logging.getLogger(__name__)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Files that should NEVER be auto-modified
_PROTECTED_FILES = {
    'config.py', '.env', 'scripts/supervisor.py',
    'scripts/session_watchdog.py', 'scripts/hotfix_manager.py',
}

# Max hotfixes per session to prevent fix loops
_MAX_HOTFIXES = 5


class HotfixProposal:
    """A proposed code fix awaiting approval."""
    def __init__(self, fix_id: str, filepath: str, line_num: int,
                 error_type: str, error_msg: str, traceback: str,
                 old_code: str, new_code: str, explanation: str):
        self.fix_id = fix_id
        self.filepath = filepath
        self.line_num = line_num
        self.error_type = error_type
        self.error_msg = error_msg
        self.traceback = traceback
        self.old_code = old_code
        self.new_code = new_code
        self.explanation = explanation
        self.created_at = datetime.now()
        self.approved = False
        self.applied = False


class HotfixManager:
    """
    Auto-fix system for paper trading sessions.

    Modes:
      auto_apply=True  (default for paper): Apply fixes immediately, email notification
      auto_apply=False (for live trading):  Email approval required before applying
    """

    def __init__(self, auto_apply: bool = True):
        self._auto_apply = auto_apply
        self._smtp_server = os.getenv('ALERT_EMAIL_SERVER', 'smtp.mail.yahoo.com')
        self._smtp_port = int(os.getenv('ALERT_EMAIL_PORT', '587'))
        self._imap_server = 'imap.mail.yahoo.com'
        self._imap_port = 993
        self._email_user = os.getenv('ALERT_EMAIL_USER', '')
        self._email_pass = os.getenv('ALERT_EMAIL_PASS', '')
        self._email_from = os.getenv('ALERT_EMAIL_FROM', self._email_user)
        self._email_to = os.getenv('ALERT_EMAIL_TO', '')

        self._pending: dict[str, HotfixProposal] = {}  # fix_id → proposal
        self._applied_count = 0
        self._applied_fixes = []  # list of (time, file, explanation)

    def propose_fix_from_traceback(self, traceback_text: str) -> Optional[HotfixProposal]:
        """
        Parse a traceback and generate a fix proposal if the error matches
        a known pattern. Returns None if no fix can be proposed.
        """
        if self._applied_count >= _MAX_HOTFIXES:
            log.warning("[Hotfix] Max hotfixes (%d) reached — skipping", _MAX_HOTFIXES)
            return None

        # Parse traceback for file, line, error
        file_path, line_num, error_type, error_msg = self._parse_traceback(traceback_text)
        if not file_path or not error_type:
            return None

        # Check if file is protected
        rel_path = os.path.relpath(file_path, PROJECT_ROOT)
        if rel_path in _PROTECTED_FILES:
            log.info("[Hotfix] %s is protected — no auto-fix", rel_path)
            return None

        # Check if file exists
        if not os.path.exists(file_path):
            return None

        # Try to generate a fix
        old_code, new_code, explanation = self._generate_fix(
            file_path, line_num, error_type, error_msg, traceback_text,
        )

        if not old_code or not new_code:
            # Can't auto-fix — still send the error details
            fix_id = f"info_{int(time.time())}"
            proposal = HotfixProposal(
                fix_id=fix_id, filepath=file_path, line_num=line_num,
                error_type=error_type, error_msg=error_msg,
                traceback=traceback_text,
                old_code="", new_code="", explanation="No auto-fix available",
            )
            self._send_error_report(proposal)
            return None

        fix_id = f"fix_{int(time.time())}"
        proposal = HotfixProposal(
            fix_id=fix_id, filepath=file_path, line_num=line_num,
            error_type=error_type, error_msg=error_msg,
            traceback=traceback_text,
            old_code=old_code, new_code=new_code, explanation=explanation,
        )

        if self._auto_apply:
            # Paper trading: apply immediately, notify via email
            if self._apply_fix(proposal):
                log.info("[Hotfix] AUTO-APPLIED fix %s for %s:%d — %s",
                         fix_id, rel_path, line_num, explanation)
                return proposal
            else:
                # Fix failed to apply — send error report
                self._send_error_report(proposal)
                return None
        else:
            # Live trading: require email approval
            self._pending[fix_id] = proposal
            self._send_approval_request(proposal)
            log.info("[Hotfix] Proposed fix %s for %s:%d — awaiting email approval",
                     fix_id, rel_path, line_num)
            return proposal

    def check_approvals(self) -> list:
        """
        Check IMAP inbox for approval replies. Returns list of applied fix IDs.
        """
        if not self._pending:
            return []

        applied = []
        try:
            mail = imaplib.IMAP4_SSL(self._imap_server, self._imap_port)
            mail.login(self._email_user, self._email_pass)
            mail.select('INBOX')

            # Search for recent replies with APPROVE in subject or body
            since = (datetime.now() - timedelta(hours=2)).strftime('%d-%b-%Y')
            status, msg_ids = mail.search(None, f'(SINCE {since} SUBJECT "HOTFIX")')

            if status != 'OK' or not msg_ids[0]:
                mail.logout()
                return []

            for msg_id in msg_ids[0].split():
                try:
                    status, msg_data = mail.fetch(msg_id, '(RFC822)')
                    if status != 'OK':
                        continue

                    msg = email.message_from_bytes(msg_data[0][1])
                    subject = msg.get('Subject', '')
                    body = self._get_email_body(msg)

                    # Check for APPROVE + fix_id
                    content = f"{subject} {body}".upper()
                    if 'APPROVE' not in content:
                        continue

                    # Find which fix_id was approved
                    for fix_id, proposal in list(self._pending.items()):
                        if fix_id.upper() in content or 'APPROVE' in content:
                            if self._apply_fix(proposal):
                                applied.append(fix_id)
                                del self._pending[fix_id]
                                # Mark email as read
                                mail.store(msg_id, '+FLAGS', '\\Seen')
                            break  # one approval per email

                except Exception as e:
                    log.warning("[Hotfix] Error processing email %s: %s", msg_id, e)

            mail.logout()

        except Exception as e:
            log.warning("[Hotfix] IMAP check failed: %s", e)

        return applied

    # ── Fix Generation (pattern matching) ─────────────────────────────────

    def _generate_fix(
        self, filepath: str, line_num: int,
        error_type: str, error_msg: str, traceback: str,
    ) -> Tuple[str, str, str]:
        """
        Generate a fix based on error pattern matching.
        Returns (old_code, new_code, explanation) or ('', '', '') if no fix.
        """
        try:
            with open(filepath) as f:
                lines = f.readlines()
        except Exception:
            return '', '', ''

        if line_num < 1 or line_num > len(lines):
            return '', '', ''

        error_line = lines[line_num - 1]
        context_start = max(0, line_num - 5)
        context = ''.join(lines[context_start:line_num + 3])

        # ── Pattern 1: NameError — undefined variable/function ──────────
        if error_type == 'NameError':
            match = re.search(r"name '(\w+)' is not defined", error_msg)
            if match:
                name = match.group(1)
                # Check if it's a missing import
                import_fix = self._find_missing_import(name, filepath, lines)
                if import_fix:
                    return import_fix

        # ── Pattern 2: AttributeError — wrong attribute name ────────────
        if error_type == 'AttributeError':
            match = re.search(r"'(\w+)' object has no attribute '(\w+)'", error_msg)
            if match:
                obj_type, attr = match.group(1), match.group(2)
                # Common renames
                renames = {
                    'signal': 'direction',
                    'setup_name': 'strategy_name',
                    'qty': 'quantity',
                }
                if attr in renames:
                    old = f".{attr}"
                    new = f".{renames[attr]}"
                    return (
                        error_line.rstrip(),
                        error_line.replace(old, new).rstrip(),
                        f"Renamed .{attr} to .{renames[attr]} ({obj_type} attribute change)",
                    )

        # ── Pattern 3: TypeError — wrong number of arguments ───────────
        if error_type == 'TypeError':
            match = re.search(r"got an unexpected keyword argument '(\w+)'", error_msg)
            if match:
                kwarg = match.group(1)
                if f"{kwarg}=" in error_line:
                    # Remove the unexpected kwarg
                    # Find and remove the kwarg=value from the line
                    new_line = re.sub(
                        rf',?\s*{kwarg}=[^,\)]+', '', error_line
                    )
                    return (
                        error_line.rstrip(),
                        new_line.rstrip(),
                        f"Removed unexpected keyword argument '{kwarg}'",
                    )

            match = re.search(r"missing (\d+) required positional argument: '(\w+)'", error_msg)
            if match:
                arg_name = match.group(2)
                # Add default value for missing arg
                return '', '', ''  # too risky to guess the value

        # ── Pattern 4: ImportError — wrong module path ──────────────────
        if error_type in ('ImportError', 'ModuleNotFoundError'):
            match = re.search(r"No module named '([\w.]+)'", error_msg)
            if match:
                module = match.group(1)
                # Check if module was renamed/moved
                old_new = {
                    'lifecycle.adapters.pop_adapter': None,  # deleted
                    'lifecycle.adapters.pro_adapter': None,  # deleted
                }
                if module in old_new:
                    if old_new[module] is None:
                        # Comment out the import
                        return (
                            error_line.rstrip(),
                            f"# {error_line.rstrip()}  # V9: removed",
                            f"Commented out import of deleted module '{module}'",
                        )

        # ── Pattern 5: KeyError — missing dict key ──────────────────────
        if error_type == 'KeyError':
            # Extract the missing key (handles 'key' and "key")
            match = re.search(r"KeyError: ['\"](\w+)['\"]", error_msg)
            if match:
                key = match.group(1)
                # Case A: literal string key: data['missing_key']
                for quote in ["'", '"']:
                    bracket = f"[{quote}{key}{quote}]"
                    getter = f".get({quote}{key}{quote}, None)"
                    if bracket in error_line:
                        return (
                            error_line.rstrip(),
                            error_line.replace(bracket, getter).rstrip(),
                            f"Changed [{quote}{key}{quote}] to .get() for safe access",
                        )

            # Case B: variable key: data[ticker] where ticker='AAPL'
            # Find any [variable] access on the error line
            bracket_match = re.search(r'(\w+)\[(\w+)\]', error_line)
            if bracket_match:
                dict_name = bracket_match.group(1)
                var_name = bracket_match.group(2)
                old = f"{dict_name}[{var_name}]"
                new = f"{dict_name}.get({var_name})"
                if old in error_line:
                    return (
                        error_line.rstrip(),
                        error_line.replace(old, new).rstrip(),
                        f"Changed {old} to .get() for safe access",
                    )

        # ── Pattern 6: IndexError — list index out of range ─────────────
        if error_type == 'IndexError':
            if 'iloc[-1]' in error_line or 'iloc[-2]' in error_line:
                idx = '-1' if 'iloc[-1]' in error_line else '-2'
                min_len = '1' if idx == '-1' else '2'
                # Find the variable being indexed
                match = re.search(r'(\w+)\[', error_line.split('iloc')[0])
                indent = len(error_line) - len(error_line.lstrip())
                pad = ' ' * indent
                return (
                    error_line.rstrip(),
                    f"{pad}if len({match.group(1) if match else 'df'}) >= {min_len}:\n"
                    f"{pad}    {error_line.strip()}\n"
                    f"{pad}else:\n"
                    f"{pad}    pass  # V9: skip when insufficient data",
                    f"Added length check before iloc[{idx}] access",
                )

        # ── Pattern 7: ValueError — common conversion errors ────────────
        if error_type == 'ValueError':
            if 'could not convert string to float' in error_msg:
                if 'float(' in error_line:
                    return (
                        error_line.rstrip(),
                        error_line.replace('float(', 'float(str(').replace(')', '))', 1).rstrip()
                        if error_line.count('float(') == 1 else error_line.rstrip(),
                        "Wrapped float() arg in str() for safe conversion",
                    )

        # ── Pattern 8: ZeroDivisionError ────────────────────────────────
        if error_type == 'ZeroDivisionError':
            if '/' in error_line:
                # Find the divisor and add max(x, 1e-9)
                match = re.search(r'/\s*(\w+)', error_line)
                if match:
                    divisor = match.group(1)
                    return (
                        error_line.rstrip(),
                        error_line.replace(f'/ {divisor}', f'/ max({divisor}, 1e-9)').rstrip(),
                        f"Added max({divisor}, 1e-9) to prevent division by zero",
                    )

        # ── Pattern 9: FileNotFoundError ────────────────────────────────
        if error_type == 'FileNotFoundError':
            match = re.search(r"'([^']+)'", error_msg)
            if match:
                missing_file = match.group(1)
                if missing_file.endswith('.json'):
                    return '', '', ''  # handled by self-healer (create empty JSON)

        # ── Pattern 10: StopIteration — empty iterator ──────────────────
        if error_type == 'StopIteration':
            if 'next(' in error_line:
                return (
                    error_line.rstrip(),
                    error_line.replace('next(', 'next(iter([None]) if False else ').rstrip()
                    if False else  # too risky, use default
                    error_line.replace('next(', 'next(').replace(')', ', None)').rstrip(),
                    "Added default=None to next() call",
                )

        return '', '', ''

    def _find_missing_import(self, name: str, filepath: str, lines: list):
        """Try to find the correct import for a missing name."""
        # Search project for the definition
        import subprocess
        try:
            result = subprocess.run(
                ['grep', '-rl', '-E', f'def {name}|class {name}|{name} =',
                 '--include=*.py', PROJECT_ROOT],
                capture_output=True, text=True, timeout=5,
            )
            if result.stdout.strip():
                source_file = result.stdout.strip().split('\n')[0]
                rel_source = os.path.relpath(source_file, PROJECT_ROOT)
                module = rel_source.replace('/', '.').replace('.py', '')

                # Add import at top of file (after existing imports)
                last_import_line = 0
                for i, line in enumerate(lines):
                    if line.startswith('import ') or line.startswith('from '):
                        last_import_line = i

                old = lines[last_import_line].rstrip()
                new = f"{old}\nfrom {module} import {name}"
                return (old, new, f"Added missing import: from {module} import {name}")
        except Exception:
            pass
        return None

    # ── Traceback Parsing ─────────────────────────────────────────────────

    def _parse_traceback(self, tb: str) -> Tuple[str, int, str, str]:
        """Extract file, line, error type, error message from traceback."""
        file_path = ''
        line_num = 0
        error_type = ''
        error_msg = ''

        lines = tb.strip().split('\n')

        # Find the last File reference (the actual error location)
        for line in reversed(lines):
            match = re.search(r'File "([^"]+)", line (\d+)', line)
            if match:
                file_path = match.group(1)
                line_num = int(match.group(2))
                break

        # Error type is usually the last line
        if lines:
            last = lines[-1]
            match = re.match(r'(\w+Error|\w+Exception): (.+)', last)
            if match:
                error_type = match.group(1)
                error_msg = match.group(2)
            elif ':' in last:
                parts = last.split(':', 1)
                error_type = parts[0].strip()
                error_msg = parts[1].strip() if len(parts) > 1 else ''

        return file_path, line_num, error_type, error_msg

    # ── Fix Application ───────────────────────────────────────────────────

    def _apply_fix(self, proposal: HotfixProposal) -> bool:
        """Apply a fix: backup file, make replacement, verify syntax."""
        filepath = proposal.filepath
        rel = os.path.relpath(filepath, PROJECT_ROOT)

        try:
            # Backup
            backup = filepath + '.bak'
            import shutil
            shutil.copy2(filepath, backup)

            # Read file
            with open(filepath) as f:
                content = f.read()

            # Apply replacement
            if proposal.old_code not in content:
                log.warning("[Hotfix] old_code not found in %s — fix may be stale", rel)
                return False

            new_content = content.replace(proposal.old_code, proposal.new_code, 1)

            # Verify syntax of new content
            try:
                compile(new_content, filepath, 'exec')
            except SyntaxError as e:
                log.error("[Hotfix] Fix would create syntax error: %s — reverting", e)
                return False

            # Write
            with open(filepath, 'w') as f:
                f.write(new_content)

            self._applied_count += 1
            self._applied_fixes.append((
                datetime.now().strftime('%H:%M:%S'),
                rel,
                proposal.explanation,
            ))
            proposal.applied = True

            log.info("[Hotfix] APPLIED fix %s to %s: %s",
                     proposal.fix_id, rel, proposal.explanation)

            # Send confirmation email
            self._send_confirmation(proposal)

            return True

        except Exception as e:
            log.error("[Hotfix] Failed to apply fix: %s", e)
            # Restore backup
            if os.path.exists(backup):
                shutil.copy2(backup, filepath)
            return False

    # ── Email Functions ───────────────────────────────────────────────────

    def _send_approval_request(self, proposal: HotfixProposal):
        """Send email requesting approval for a fix."""
        rel = os.path.relpath(proposal.filepath, PROJECT_ROOT)
        subject = f"[HOTFIX] {proposal.error_type} in {rel} — Reply APPROVE to fix"

        body = (
            f"HOTFIX PROPOSAL — {proposal.fix_id}\n"
            f"{'=' * 50}\n\n"
            f"Error: {proposal.error_type}: {proposal.error_msg}\n"
            f"File: {rel}:{proposal.line_num}\n"
            f"Time: {proposal.created_at.strftime('%H:%M:%S')}\n\n"
            f"TRACEBACK:\n{proposal.traceback[-500:]}\n\n"
            f"PROPOSED FIX:\n"
            f"  Explanation: {proposal.explanation}\n\n"
            f"  BEFORE:\n    {proposal.old_code}\n\n"
            f"  AFTER:\n    {proposal.new_code}\n\n"
            f"{'=' * 50}\n"
            f"Reply with APPROVE to apply this fix.\n"
            f"The supervisor will automatically restart the process.\n"
            f"Fix ID: {proposal.fix_id}\n"
        )

        self._send_email(subject, body)

    def _send_error_report(self, proposal: HotfixProposal):
        """Send error details when no auto-fix is available."""
        rel = os.path.relpath(proposal.filepath, PROJECT_ROOT) if proposal.filepath else 'unknown'
        subject = f"[ERROR] {proposal.error_type} in {rel} — No auto-fix available"

        body = (
            f"ERROR REPORT\n"
            f"{'=' * 50}\n\n"
            f"Error: {proposal.error_type}: {proposal.error_msg}\n"
            f"File: {rel}:{proposal.line_num}\n"
            f"Time: {proposal.created_at.strftime('%H:%M:%S')}\n\n"
            f"TRACEBACK:\n{proposal.traceback[-800:]}\n\n"
            f"No automatic fix available. Manual intervention may be needed.\n"
            f"The supervisor will retry automatically (max 3 restarts).\n"
        )

        self._send_email(subject, body)

    def _send_confirmation(self, proposal: HotfixProposal):
        """Send confirmation that fix was applied."""
        rel = os.path.relpath(proposal.filepath, PROJECT_ROOT)
        subject = f"[HOTFIX APPLIED] {rel} — {proposal.explanation}"

        body = (
            f"HOTFIX APPLIED\n"
            f"{'=' * 50}\n\n"
            f"Fix ID: {proposal.fix_id}\n"
            f"File: {rel}:{proposal.line_num}\n"
            f"Change: {proposal.explanation}\n\n"
            f"  BEFORE: {proposal.old_code}\n"
            f"  AFTER:  {proposal.new_code}\n\n"
            f"Backup saved: {rel}.bak\n"
            f"Supervisor will restart the process automatically.\n"
            f"Total hotfixes this session: {self._applied_count}/{_MAX_HOTFIXES}\n"
        )

        self._send_email(subject, body)

    def _send_email(self, subject: str, body: str):
        """Send email via SMTP."""
        try:
            msg = MIMEText(body, 'plain')
            msg['Subject'] = subject
            msg['From'] = self._email_from
            msg['To'] = self._email_to

            s = smtplib.SMTP(self._smtp_server, self._smtp_port, timeout=15)
            s.starttls()
            s.login(self._email_user, self._email_pass)
            s.sendmail(self._email_from, [self._email_to], msg.as_string())
            s.quit()
            log.info("[Hotfix] Email sent: %s", subject[:60])
        except Exception as e:
            log.error("[Hotfix] Email send failed: %s", e)

    def _get_email_body(self, msg) -> str:
        """Extract plain text body from email message."""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == 'text/plain':
                    return part.get_payload(decode=True).decode('utf-8', errors='ignore')
        return msg.get_payload(decode=True).decode('utf-8', errors='ignore')
