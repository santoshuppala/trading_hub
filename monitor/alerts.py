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


def _alert_worker() -> None:
    """Daemon thread: drain the queue and send each email."""
    while True:
        item = _alert_queue.get()
        if item is None:          # sentinel — stop signal
            break
        alert_email, message = item
        _deliver(alert_email, message)
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

def _deliver(alert_email: str, message: str) -> None:
    """Blocking SMTP call — executed exclusively in the alert-smtp daemon thread."""
    email_user  = os.getenv('ALERT_EMAIL_USER')
    email_pass  = os.getenv('ALERT_EMAIL_PASS')
    email_from  = os.getenv('ALERT_EMAIL_FROM') or email_user
    smtp_server = os.getenv('ALERT_EMAIL_SERVER') or 'smtp.mail.yahoo.com'
    smtp_port   = int(os.getenv('ALERT_EMAIL_PORT') or 587)

    if not email_user or not email_pass or not email_from:
        log.warning(
            'Alert skipped: missing SMTP settings. '
            'Set ALERT_EMAIL_USER, ALERT_EMAIL_PASS, and ALERT_EMAIL_FROM.'
        )
        log.info(f'ALERT: {message}')
        return

    try:
        msg = MIMEText(message)
        msg['Subject'] = 'Trading Alert'
        msg['From']    = email_from
        msg['To']      = alert_email

        server = smtplib.SMTP(smtp_server, smtp_port, timeout=10)
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(email_user, email_pass)
        server.sendmail(email_from, alert_email, msg.as_string())
        server.quit()
        log.info(f"Alert sent: {message}")
    except smtplib.SMTPAuthenticationError as e:
        log.error(f"Failed to send alert: SMTP authentication failed: {e}")
        log.warning(
            'Check ALERT_EMAIL_USER and ALERT_EMAIL_PASS, '
            'and if using Gmail enable an App Password.'
        )
    except Exception as e:
        log.error(f"Failed to send alert: {e}")
