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
import logging
import os
import queue
import smtplib
import threading
from email.mime.text import MIMEText

log = logging.getLogger(__name__)

# ── Background delivery ───────────────────────────────────────────────────────

_alert_queue: queue.Queue = queue.Queue(maxsize=200)
_worker_started = False
_worker_lock = threading.Lock()
_consecutive_failures = 0
_backoff_until = 0.0


def _alert_worker() -> None:
    """Daemon thread: drain the queue and send each email."""
    import time
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

        # Try with connection reuse, fall back to new connection
        if server is None:
            server = _get_smtp_connection()

        success = _deliver_with_connection(server, alert_email, message)
        if not success:
            # Connection failed — increment backoff counter
            _consecutive_failures += 1
            # Exponential backoff: 2^failures seconds (1s, 2s, 4s, 8s, 16s max)
            backoff_secs = min(2 ** _consecutive_failures, 16)
            _backoff_until = time.time() + backoff_secs
            log.warning(
                f"Alert delivery failed — backoff {backoff_secs}s "
                f"({_consecutive_failures} consecutive failures)"
            )
            server = None
        else:
            # Success — reset failure counter
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

def send_alert(alert_email, message):
    """
    Queue an email alert for asynchronous SMTP delivery.

    Returns immediately.  Never blocks the calling thread.
    If alert_email is None or falsy, logs the message instead.
    """
    if not alert_email:
        log.info(f"ALERT: {message}")
        return
    _ensure_worker()
    try:
        _alert_queue.put_nowait((alert_email, message))
    except queue.Full:
        log.warning(f"Alert queue full — dropping: {message[:120]}")


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
