"""
Email alert delivery.

send_alert() enqueues a message and returns immediately — SMTP runs in a
background daemon thread so it can never stall the trading loop.

Configuration (env vars):
  ALERT_EMAIL_USER   — SMTP login username
  ALERT_EMAIL_PASS   — SMTP login password (Yahoo 16-char app password)
  ALERT_EMAIL_FROM   — From address (defaults to ALERT_EMAIL_USER)
  ALERT_EMAIL_SERVER — SMTP server hostname (default: smtp.mail.yahoo.com)
  ALERT_EMAIL_PORT   — SMTP port (default: 587)

If alert_email is None/falsy the message is written to the log instead.
If the queue is full (>200 pending) the alert is dropped and logged.
"""
import hashlib
import logging
import os
import queue
import smtplib
import threading
import time
from email.mime.text import MIMEText

log = logging.getLogger(__name__)

# ── Severity levels ──────────────────────────────────────────────────────────

CRITICAL = 0
WARNING  = 1
INFO     = 2

# Rate limits in seconds per severity
_RATE_LIMITS = {
    'CRITICAL': 0,       # no rate limit — always send
    'WARNING':  900,     # 15 minutes
    'INFO':     3600,    # 1 hour
}

# Tracks last send time per unique message hash: {msg_hash: timestamp}
_alert_last_sent: dict[str, float] = {}

# ── Background delivery ───────────────────────────────────────────────────────

_alert_queue: queue.Queue = queue.Queue(maxsize=200)
_worker_started = False
_worker_lock = threading.Lock()
_consecutive_failures = 0
_backoff_until = 0.0
_worker_disabled_until = 0.0  # V8: auto-recover after 30 min cooldown


def _alert_worker() -> None:
    """Daemon thread: drain the queue and send each email."""
    global _consecutive_failures, _backoff_until

    server = None
    last_send_time = 0.0

    while True:
        item = _alert_queue.get()
        if item is None:          # sentinel — stop signal
            break
        alert_email, message = item

        now = time.time()

        # Exponential backoff: if many consecutive failures, wait before retry
        if now < _backoff_until:
            wait_time = _backoff_until - now
            log.warning(
                f"Alert backoff active ({wait_time:.1f}s remaining) — "
                f"{_consecutive_failures} consecutive failures"
            )
            time.sleep(min(wait_time, 5.0))  # cap wait at 5s per message

        # Rate limiting: minimum 1 second between sends
        elapsed = time.time() - last_send_time
        if elapsed < 1.0:
            time.sleep(1.0 - elapsed)

        # Fresh connection per send — Yahoo drops idle connections
        server = _get_smtp_connection()

        success = _deliver_with_connection(server, alert_email, message)

        # Always close after send to avoid stale connections
        if server:
            try:
                server.quit()
            except Exception:
                pass
        server = None

        if not success:
            _consecutive_failures += 1
            backoff_secs = min(2 ** _consecutive_failures, 16)
            _backoff_until = time.time() + backoff_secs
            log.error(
                f"Alert delivery failed — backoff {backoff_secs}s "
                f"({_consecutive_failures} consecutive failures)"
            )
            # V9 (R6): Reduced blackout from 30 min to 5 min.
            # CRITICAL alerts bypass blackout entirely.
            if _consecutive_failures >= 10:
                _worker_disabled_until = time.time() + 300  # 5 min cooldown (was 30 min)
                log.critical(
                    "Alert delivery paused after %d consecutive failures. "
                    "Will retry in 5 min. CRITICAL alerts still attempt delivery.",
                    _consecutive_failures)
                # Drain queue — log all, attempt delivery for CRITICAL only
                while not _alert_queue.empty():
                    try:
                        item = _alert_queue.get_nowait()
                        if item:
                            _email, _msg = item[0], item[1]
                            _sev = item[2] if len(item) > 2 else 'INFO'
                            if _sev == 'CRITICAL':
                                try:
                                    _send_email(_email, _msg, severity='CRITICAL')
                                    log.info("CRITICAL alert sent despite blackout: %s", _msg[:100])
                                except Exception:
                                    log.info("ALERT (CRITICAL, delivery failed): %s", _msg[:200])
                            else:
                                log.info("ALERT (email paused): %s", _msg[:200])
                        _alert_queue.task_done()
                    except Exception:
                        break
                # Sleep for cooldown then reset and continue
                time.sleep(300)
                _consecutive_failures = 0
                _backoff_until = 0.0
                _worker_disabled_until = 0.0
                log.info("Alert delivery resuming after 5 min cooldown")
                continue  # resume the worker loop
        else:
            _consecutive_failures = 0
            _backoff_until = 0.0

        last_send_time = time.time()
        _alert_queue.task_done()


def _ensure_worker() -> None:
    global _worker_started
    if _worker_started:
        return
    with _worker_lock:
        if not _worker_started:
            t = threading.Thread(target=_alert_worker, daemon=True, name='alert-smtp')
            t.start()
            _worker_started = True


# ── Public API ────────────────────────────────────────────────────────────────

def send_alert(alert_email, message, severity='INFO'):
    """
    Queue an email alert for asynchronous SMTP delivery.

    Returns immediately.  Never blocks the calling thread.
    If alert_email is None or falsy, logs the message instead.

    Severity levels control rate-limiting and subject tagging:
      'CRITICAL' — send immediately, no rate limit, subject prefixed [CRITICAL]
      'WARNING'  — max 1 per 15 min per unique message, subject prefixed [WARNING]
      'INFO'     — max 1 per hour per unique message (default)
    """
    severity = severity.upper()
    if severity not in _RATE_LIMITS:
        severity = 'INFO'

    log.debug("Alert attempt [%s]: %s", severity, message[:120])

    # ── Rate limiting (CRITICAL always passes) ────────────────────────────
    msg_hash = hashlib.md5(message.encode()).hexdigest()[:16]
    now = time.time()
    rate_limit = _RATE_LIMITS[severity]

    if rate_limit > 0 and msg_hash in _alert_last_sent:
        elapsed = now - _alert_last_sent[msg_hash]
        if elapsed < rate_limit:
            log.debug(
                "Alert rate-limited [%s]: %s (last sent %ds ago, limit %ds)",
                severity, message[:80], int(elapsed), rate_limit,
            )
            return

    _alert_last_sent[msg_hash] = now

    # ── Prepend severity tag ──────────────────────────────────────────────
    if severity == 'CRITICAL':
        tagged_message = f"[CRITICAL] {message}"
    elif severity == 'WARNING':
        tagged_message = f"[WARNING] {message}"
    else:
        tagged_message = message

    log.info("Alert queued [%s]: %s", severity, message[:120])

    if not alert_email:
        log.info(f"ALERT: {tagged_message}")
        return

    # V8: If worker is in cooldown, log instead of queuing
    if _worker_disabled_until > 0 and time.time() < _worker_disabled_until:
        log.info("ALERT (email paused): %s", tagged_message[:200])
        return

    _ensure_worker()
    try:
        _alert_queue.put_nowait((alert_email, tagged_message))
    except queue.Full:
        log.warning(f"Alert queue full — dropping: {tagged_message[:120]}")


# ── SMTP delivery (runs in background thread only) ────────────────────────────

def _get_smtp_connection():
    """Create a new SMTP connection. Returns None on failure."""
    email_user  = os.getenv('ALERT_EMAIL_USER')
    email_pass  = os.getenv('ALERT_EMAIL_PASS')
    smtp_server = os.getenv('ALERT_EMAIL_SERVER') or 'smtp.mail.yahoo.com'
    smtp_port   = int(os.getenv('ALERT_EMAIL_PORT') or 587)

    if not email_user or not email_pass:
        return None

    try:
        server = smtplib.SMTP(smtp_server, smtp_port, timeout=10)
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(email_user, email_pass)
        return server
    except smtplib.SMTPAuthenticationError as e:
        log.error(f"SMTP auth failed: {e}")
        log.warning(
            'Check ALERT_EMAIL_USER and ALERT_EMAIL_PASS, '
            'and if using Gmail enable an App Password.'
        )
        return None
    except (ConnectionRefusedError, ConnectionResetError, TimeoutError) as e:
        log.warning(f"SMTP connection refused: {e} — server may be rate-limiting")
        return None
    except smtplib.SMTPException as e:
        log.warning(f"SMTP server error: {e} — will backoff and retry")
        return None
    except Exception as e:
        log.warning(f"SMTP connection failed: {e}")
        return None


def _deliver_with_connection(server, alert_email: str, message: str) -> bool:
    """
    Send email using existing connection.
    Returns True on success, False if connection failed.
    """
    email_user  = os.getenv('ALERT_EMAIL_USER')
    email_from  = os.getenv('ALERT_EMAIL_FROM') or email_user

    if not email_user or not email_from:
        log.info(f'ALERT: {message}')
        return True

    if server is None:
        log.error(f"Failed to send alert: no SMTP connection")
        return False

    try:
        msg = MIMEText(message)
        # Derive subject from severity tag in message body
        if message.startswith('[CRITICAL]'):
            msg['Subject'] = '[CRITICAL] Trading Alert'
        elif message.startswith('[WARNING]'):
            msg['Subject'] = '[WARNING] Trading Alert'
        else:
            msg['Subject'] = 'Trading Alert'
        msg['From']    = email_from
        msg['To']      = alert_email
        server.sendmail(email_from, alert_email, msg.as_string())
        log.info(f"Alert sent: {message}")
        return True
    except smtplib.SMTPException as e:
        log.error(f"Failed to send alert: {e}")
        return False
    except Exception as e:
        log.error(f"Failed to send alert: {e}")
        return False


def validate_smtp() -> bool:
    """V8: Test SMTP credentials at startup. Returns True if working."""
    server = _get_smtp_connection()
    if server is None:
        log.critical("SMTP validation FAILED — alerts will not be delivered! "
                     "Check ALERT_EMAIL_USER / ALERT_EMAIL_PASS.")
        return False
    try:
        server.quit()
    except Exception:
        pass
    log.info("SMTP validation OK — alerts will be delivered")
    return True


def _deliver(alert_email: str, message: str) -> None:
    """Legacy function — kept for backward compatibility."""
    if not alert_email:
        log.info(f"ALERT: {message}")
        return
    server = _get_smtp_connection()
    _deliver_with_connection(server, alert_email, message)
    if server:
        try:
            server.quit()
        except:
            pass
