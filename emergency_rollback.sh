#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# EMERGENCY ROLLBACK — Reverts trading hub to V9 Baseline (b838c04)
# Tagged as: v9-baseline
#
# Usage:
#   ./emergency_rollback.sh          # Full rollback: stop processes + revert code
#   ./emergency_rollback.sh --dry-run # Show what would happen without doing it
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail

BASELINE_TAG="v9-baseline"
BASELINE_COMMIT="b838c04"
PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$PROJECT_ROOT/logs"
ROLLBACK_LOG="$LOG_DIR/rollback_$(date +%Y%m%d_%H%M%S).log"

DRY_RUN=false
if [ "${1:-}" = "--dry-run" ]; then
    DRY_RUN=true
fi

# ── Logging ──────────────────────────────────────────────────────────────
mkdir -p "$LOG_DIR"

log() {
    local msg="$(date '+%Y-%m-%d %H:%M:%S') $1"
    echo "$msg"
    if [ "$DRY_RUN" = false ]; then
        echo "$msg" >> "$ROLLBACK_LOG"
    fi
}

log "═══ EMERGENCY ROLLBACK INITIATED ═══"
log "Target: $BASELINE_TAG ($BASELINE_COMMIT)"
log "Dry run: $DRY_RUN"

# ── Verify baseline exists ───────────────────────────────────────────────
cd "$PROJECT_ROOT"
if ! git rev-parse "$BASELINE_TAG" >/dev/null 2>&1; then
    log "ERROR: Tag '$BASELINE_TAG' not found. Falling back to commit hash."
    if ! git rev-parse "$BASELINE_COMMIT" >/dev/null 2>&1; then
        log "FATAL: Baseline commit $BASELINE_COMMIT not found. Abort."
        exit 1
    fi
    ROLLBACK_REF="$BASELINE_COMMIT"
else
    ROLLBACK_REF="$BASELINE_TAG"
fi

log "Rollback ref resolved: $(git rev-parse --short "$ROLLBACK_REF")"

# ── Step 1: Stop all trading processes ───────────────────────────────────
log "── Step 1: Stopping trading processes ──"

stop_processes() {
    local stopped=0

    # Kill supervisor and all children
    for proc in supervisor run_core run_options run_data_collector session_watchdog; do
        pids=$(pgrep -f "$proc" 2>/dev/null || true)
        if [ -n "$pids" ]; then
            log "Stopping $proc (PIDs: $pids)"
            if [ "$DRY_RUN" = false ]; then
                echo "$pids" | xargs kill -TERM 2>/dev/null || true
            fi
            stopped=$((stopped + 1))
        fi
    done

    if [ "$DRY_RUN" = false ] && [ "$stopped" -gt 0 ]; then
        sleep 3
        # Force kill any survivors
        for proc in supervisor run_core run_options run_data_collector session_watchdog; do
            pids=$(pgrep -f "$proc" 2>/dev/null || true)
            if [ -n "$pids" ]; then
                log "Force killing $proc (PIDs: $pids)"
                echo "$pids" | xargs kill -9 2>/dev/null || true
            fi
        done
    fi

    # Remove stale lock file
    if [ -f "$PROJECT_ROOT/.monitor.lock" ]; then
        log "Removing stale .monitor.lock"
        if [ "$DRY_RUN" = false ]; then
            rm -f "$PROJECT_ROOT/.monitor.lock"
        fi
    fi

    log "All trading processes stopped (killed $stopped process groups)"
}

stop_processes

# ── Step 2: Save current state ───────────────────────────────────────────
log "── Step 2: Saving current state ──"

CURRENT_COMMIT=$(git rev-parse --short HEAD)
CURRENT_BRANCH=$(git branch --show-current)
log "Current state: branch=$CURRENT_BRANCH commit=$CURRENT_COMMIT"

if [ "$DRY_RUN" = false ]; then
    # Stash any uncommitted work
    if ! git diff --quiet 2>/dev/null || ! git diff --cached --quiet 2>/dev/null; then
        log "Stashing uncommitted changes (retrievable via 'git stash pop')"
        git stash push -m "pre-rollback-$(date +%Y%m%d_%H%M%S)"
    else
        log "Working tree clean, nothing to stash"
    fi
fi

# ── Step 3: Rollback code ────────────────────────────────────────────────
log "── Step 3: Rolling back to $ROLLBACK_REF ──"

if [ "$DRY_RUN" = true ]; then
    log "[DRY RUN] Would reset to: $(git log --oneline -1 "$ROLLBACK_REF")"
    log "[DRY RUN] Files changed since baseline:"
    git diff --stat "$ROLLBACK_REF"..HEAD
else
    git checkout "$ROLLBACK_REF" -- .
    log "Code reverted to $ROLLBACK_REF"
fi

# ── Step 4: Clear pycache ────────────────────────────────────────────────
log "── Step 4: Clearing __pycache__ ──"
if [ "$DRY_RUN" = false ]; then
    find "$PROJECT_ROOT" -path ./venv -prune -o -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
    log "Cleared all __pycache__ directories"
fi

# ── Step 5: Verify rollback ─────────────────────────────────────────────
log "── Step 5: Verification ──"

if [ "$DRY_RUN" = false ]; then
    # Verify key files match baseline
    MISMATCH=0
    for key_file in scripts/supervisor.py scripts/run_core.py monitor/monitor.py options/engine.py config.py; do
        if [ -f "$key_file" ]; then
            if ! git diff "$ROLLBACK_REF" -- "$key_file" | grep -q '^'; then
                log "  ✓ $key_file matches baseline"
            else
                log "  ✗ $key_file DIFFERS from baseline"
                MISMATCH=$((MISMATCH + 1))
            fi
        fi
    done

    if [ "$MISMATCH" -gt 0 ]; then
        log "WARNING: $MISMATCH files don't match baseline!"
    else
        log "All key files verified against baseline"
    fi
fi

# ── Step 6: Restart (optional) ───────────────────────────────────────────
log "── Step 6: Ready to restart ──"
log ""
log "Rollback complete. To restart trading:"
log "  MONITOR_MODE=supervisor ./start_monitor.sh"
log ""
log "To undo this rollback:"
log "  git checkout $CURRENT_BRANCH"
log "  git stash pop  (if changes were stashed)"
log ""
log "═══ ROLLBACK FINISHED ═══"
