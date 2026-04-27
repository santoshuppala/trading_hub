"""
Claude Fix Agent — invokes Claude to analyze crashes and create GitHub PRs.

Flow:
  1. Watchdog detects repeated crash (3x same error)
  2. Agent reads traceback + source file context
  3. Calls Claude API with structured prompt
  4. Claude returns: root cause, fix diff, impact assessment
  5. Agent creates git branch, applies fix, pushes, opens PR
  6. Emails operator with PR link

Safety:
  - NEVER applies fix to running code (creates PR only)
  - NEVER modifies: .env, config.py, data/, state files
  - Branch naming: fix/watchdog-{engine}-{timestamp}
  - PR body includes impact assessment + rollback instructions
  - Max 3 PRs per day (prevents spam)
  - Requires ANTHROPIC_API_KEY and GITHUB_TOKEN env vars

Usage:
    Called by SessionWatchdog._heal_diagnose_crash — not run standalone.
"""
import json
import logging
import os
import re
import subprocess
import time
from datetime import datetime
from typing import Optional, Tuple

log = logging.getLogger(__name__)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Files that should NEVER be in a fix PR
_PROTECTED_PATTERNS = {
    '.env', 'config.py', 'data/', 'logs/',
    'scripts/supervisor.py', 'scripts/outer_watchdog.py',
    'scripts/claude_fix_agent.py',
}

_MAX_PRS_PER_DAY = 3
_STATE_FILE = os.path.join(PROJECT_ROOT, 'data', 'claude_fix_agent_state.json')


class ClaudeFixAgent:
    """Analyzes crashes via Claude API and creates fix PRs."""

    def __init__(self):
        self._api_key = os.getenv('ANTHROPIC_API_KEY', '')
        self._github_token = os.getenv('GITHUB_TOKEN', '')
        self._enabled = bool(self._api_key and self._github_token)

        if not self._enabled:
            log.info("[ClaudeFixAgent] Disabled — set ANTHROPIC_API_KEY + GITHUB_TOKEN to enable")

        self._state = self._load_state()

    def _load_state(self) -> dict:
        try:
            if os.path.exists(_STATE_FILE):
                with open(_STATE_FILE) as f:
                    state = json.load(f)
                if state.get('date') != datetime.now().strftime('%Y-%m-%d'):
                    return {'date': datetime.now().strftime('%Y-%m-%d'), 'prs_today': 0, 'fixes': []}
                return state
        except Exception:
            pass
        return {'date': datetime.now().strftime('%Y-%m-%d'), 'prs_today': 0, 'fixes': []}

    def _save_state(self):
        try:
            with open(_STATE_FILE, 'w') as f:
                json.dump(self._state, f, indent=2)
        except Exception:
            pass

    def propose_fix(self, engine: str, traceback_text: str) -> Optional[str]:
        """Analyze crash and create a GitHub PR with fix.

        Returns PR URL on success, None on failure.
        """
        if not self._enabled:
            return None

        if self._state['prs_today'] >= _MAX_PRS_PER_DAY:
            log.warning("[ClaudeFixAgent] Max %d PRs/day reached — skipping", _MAX_PRS_PER_DAY)
            return None

        # Deduplicate: don't create PR for same error twice
        error_sig = self._error_signature(traceback_text)
        if error_sig in [f.get('sig') for f in self._state.get('fixes', [])]:
            log.info("[ClaudeFixAgent] Already created PR for this error — skipping")
            return None

        try:
            # 1. Extract file + line from traceback
            filepath, line_num = self._extract_location(traceback_text)
            if not filepath:
                log.warning("[ClaudeFixAgent] Could not extract file location from traceback")
                return None

            # 2. Read source context (50 lines around the error)
            source_context = self._read_context(filepath, line_num)

            # 3. Call Claude API
            analysis = self._call_claude(engine, traceback_text, filepath, line_num, source_context)
            if not analysis:
                return None

            # 4. Validate the fix doesn't touch protected files
            if not self._validate_fix(analysis):
                return None

            # 5. Create branch, apply fix, push, create PR
            pr_url = self._create_pr(engine, analysis, traceback_text)
            if pr_url:
                self._state['prs_today'] += 1
                self._state['fixes'].append({
                    'sig': error_sig,
                    'engine': engine,
                    'file': filepath,
                    'pr_url': pr_url,
                    'time': datetime.now().strftime('%H:%M:%S'),
                })
                self._save_state()
                log.info("[ClaudeFixAgent] PR created: %s", pr_url)

            return pr_url

        except Exception as e:
            log.warning("[ClaudeFixAgent] Failed: %s", e)
            return None

    def _error_signature(self, traceback_text: str) -> str:
        """Create a dedup key from the error type + location."""
        lines = traceback_text.strip().split('\n')
        if lines:
            last = lines[-1][:100]
            # Include file:line if available
            for line in reversed(lines):
                if 'File "' in line:
                    return f"{line.strip()[:80]}|{last}"
            return last
        return traceback_text[:100]

    def _extract_location(self, traceback_text: str) -> Tuple[Optional[str], int]:
        """Extract the file path and line number from a Python traceback."""
        # Find the last 'File "..."' line (closest to the error)
        matches = re.findall(r'File "([^"]+)", line (\d+)', traceback_text)
        if not matches:
            return None, 0

        # Filter to project files only
        for filepath, line_str in reversed(matches):
            if PROJECT_ROOT in filepath and '/venv/' not in filepath:
                return filepath, int(line_str)

        return None, 0

    def _read_context(self, filepath: str, line_num: int, window: int = 25) -> str:
        """Read source code around the error line."""
        try:
            with open(filepath) as f:
                lines = f.readlines()
            start = max(0, line_num - window - 1)
            end = min(len(lines), line_num + window)
            numbered = []
            for i, line in enumerate(lines[start:end], start=start + 1):
                marker = '>>>' if i == line_num else '   '
                numbered.append(f"{marker} {i:4d} | {line.rstrip()}")
            return '\n'.join(numbered)
        except Exception:
            return f"(could not read {filepath})"

    def _call_claude(self, engine: str, traceback_text: str,
                     filepath: str, line_num: int, source_context: str) -> Optional[dict]:
        """Call Claude API to analyze the crash and propose a fix."""
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=self._api_key)
        except ImportError:
            log.warning("[ClaudeFixAgent] anthropic package not installed")
            return None

        rel_path = os.path.relpath(filepath, PROJECT_ROOT)

        prompt = f"""You are a senior Python developer fixing a crash in a live trading system.

CRASH in engine: {engine}

TRACEBACK:
{traceback_text}

FILE: {rel_path} (line {line_num})

SOURCE CONTEXT:
{source_context}

RULES:
1. Fix ONLY the crash — do not refactor, optimize, or improve other code
2. The fix must be minimal (smallest possible change)
3. Do NOT change trading logic (stop losses, profit targets, position sizing)
4. Do NOT change: config.py, .env, data files, supervisor.py
5. If the fix requires a default value, choose the SAFEST default (one that prevents trading, not enables it)
6. If you're not confident in the fix, say so

Respond in this exact JSON format:
{{
    "root_cause": "one sentence explaining why it crashed",
    "fix_file": "{rel_path}",
    "fix_line": {line_num},
    "old_code": "exact line(s) to replace",
    "new_code": "replacement line(s)",
    "impact": "what this fix changes in trading behavior (or 'none — infrastructure only')",
    "confidence": "high/medium/low",
    "explanation": "2-3 sentences for the PR description"
}}"""

        try:
            response = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            )
            text = response.content[0].text

            # Extract JSON from response (may be wrapped in markdown)
            json_match = re.search(r'\{[\s\S]*\}', text)
            if json_match:
                analysis = json.loads(json_match.group())
                # Validate required fields
                required = ['root_cause', 'fix_file', 'old_code', 'new_code',
                            'impact', 'confidence', 'explanation']
                if all(k in analysis for k in required):
                    return analysis

            log.warning("[ClaudeFixAgent] Claude response not parseable: %s", text[:200])
            return None

        except Exception as e:
            log.warning("[ClaudeFixAgent] Claude API call failed: %s", e)
            return None

    def _validate_fix(self, analysis: dict) -> bool:
        """Validate the proposed fix doesn't touch protected files."""
        fix_file = analysis.get('fix_file', '')
        for pattern in _PROTECTED_PATTERNS:
            if pattern in fix_file:
                log.warning("[ClaudeFixAgent] Fix touches protected file: %s", fix_file)
                return False

        if analysis.get('confidence') == 'low':
            log.warning("[ClaudeFixAgent] Low confidence fix — skipping")
            return False

        return True

    def _create_pr(self, engine: str, analysis: dict, traceback_text: str) -> Optional[str]:
        """Create git branch, apply fix, push, create GitHub PR."""
        timestamp = datetime.now().strftime('%Y%m%d_%H%M')
        branch = f"fix/watchdog-{engine}-{timestamp}"

        try:
            # 1. Create branch from current HEAD
            subprocess.run(['git', 'checkout', '-b', branch],
                           cwd=PROJECT_ROOT, capture_output=True, timeout=10)

            # 2. Apply the fix
            fix_file = os.path.join(PROJECT_ROOT, analysis['fix_file'])
            if not os.path.exists(fix_file):
                log.warning("[ClaudeFixAgent] Fix file not found: %s", fix_file)
                self._cleanup_branch(branch)
                return None

            with open(fix_file) as f:
                content = f.read()

            old_code = analysis['old_code']
            new_code = analysis['new_code']

            if old_code not in content:
                log.warning("[ClaudeFixAgent] old_code not found in file — fix may be stale")
                self._cleanup_branch(branch)
                return None

            content = content.replace(old_code, new_code, 1)
            with open(fix_file, 'w') as f:
                f.write(content)

            # 3. Commit
            subprocess.run(['git', 'add', analysis['fix_file']],
                           cwd=PROJECT_ROOT, capture_output=True, timeout=10)

            commit_msg = (
                f"fix({engine}): {analysis['root_cause'][:70]}\n\n"
                f"Auto-generated by Claude Fix Agent (watchdog)\n"
                f"Confidence: {analysis['confidence']}\n"
                f"Impact: {analysis['impact']}\n\n"
                f"Root cause: {analysis['root_cause']}\n"
                f"Fix: {analysis['explanation']}"
            )
            subprocess.run(['git', 'commit', '-m', commit_msg],
                           cwd=PROJECT_ROOT, capture_output=True, timeout=10)

            # 4. Push branch
            r = subprocess.run(['git', 'push', 'origin', branch],
                               cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=30)
            if r.returncode != 0:
                log.warning("[ClaudeFixAgent] git push failed: %s", r.stderr[:200])
                self._cleanup_branch(branch)
                return None

            # 5. Create PR via gh CLI
            pr_body = (
                f"## Auto-generated fix for {engine} crash\n\n"
                f"**Root cause:** {analysis['root_cause']}\n\n"
                f"**Fix:** {analysis['explanation']}\n\n"
                f"**Impact:** {analysis['impact']}\n\n"
                f"**Confidence:** {analysis['confidence']}\n\n"
                f"### Traceback\n```\n{traceback_text[:500]}\n```\n\n"
                f"### Rollback\n```bash\n"
                f"git revert HEAD  # if merged and broken\n```\n\n"
                f"---\n"
                f"Generated by Claude Fix Agent (watchdog) at "
                f"{datetime.now().strftime('%Y-%m-%d %H:%M ET')}"
            )
            r = subprocess.run(
                ['gh', 'pr', 'create',
                 '--title', f'fix({engine}): {analysis["root_cause"][:60]}',
                 '--body', pr_body,
                 '--base', 'V9_baseline',
                 '--head', branch],
                cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=30)

            if r.returncode == 0:
                pr_url = r.stdout.strip()
                # Return to original branch
                subprocess.run(['git', 'checkout', 'V9_baseline'],
                               cwd=PROJECT_ROOT, capture_output=True, timeout=10)
                return pr_url
            else:
                log.warning("[ClaudeFixAgent] gh pr create failed: %s", r.stderr[:200])
                self._cleanup_branch(branch)
                return None

        except Exception as e:
            log.warning("[ClaudeFixAgent] PR creation failed: %s", e)
            self._cleanup_branch(branch)
            return None

    def _cleanup_branch(self, branch: str):
        """Clean up failed branch attempt."""
        try:
            subprocess.run(['git', 'checkout', 'V9_baseline'],
                           cwd=PROJECT_ROOT, capture_output=True, timeout=10)
            subprocess.run(['git', 'branch', '-D', branch],
                           cwd=PROJECT_ROOT, capture_output=True, timeout=10)
        except Exception:
            pass
