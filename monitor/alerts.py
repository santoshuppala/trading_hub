import logging
import os
import smtplib
from email.mime.text import MIMEText

log = logging.getLogger(__name__)


def send_alert(alert_email, message):
    """
    Send an email alert to alert_email.
    Reads SMTP configuration from environment variables:
      ALERT_EMAIL_USER  - SMTP login username
      ALERT_EMAIL_PASS  - SMTP login password
      ALERT_EMAIL_FROM  - From address (defaults to ALERT_EMAIL_USER)
      ALERT_EMAIL_SERVER - SMTP server hostname (defaults to smtp.mail.yahoo.com)
      ALERT_EMAIL_PORT  - SMTP port (defaults to 587)
    If alert_email is None or falsy, prints the message instead.
    """
    if not alert_email:
        log.info(f"ALERT: {message}")
        return

    email_user = os.getenv('ALERT_EMAIL_USER')
    email_pass = os.getenv('ALERT_EMAIL_PASS')
    email_from = os.getenv('ALERT_EMAIL_FROM') or email_user
    smtp_server = os.getenv('ALERT_EMAIL_SERVER') or 'smtp.mail.yahoo.com'
    smtp_port = int(os.getenv('ALERT_EMAIL_PORT') or 587)

    if not email_user or not email_pass or not email_from:
        log.warning('Alert skipped: missing SMTP settings. Set ALERT_EMAIL_USER, ALERT_EMAIL_PASS, and ALERT_EMAIL_FROM.')
        log.info(f'ALERT: {message}')
        return

    try:
        msg = MIMEText(message)
        msg['Subject'] = 'Trading Alert'
        msg['From'] = email_from
        msg['To'] = alert_email

        server = smtplib.SMTP(smtp_server, smtp_port)
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(email_user, email_pass)
        server.sendmail(email_from, alert_email, msg.as_string())
        server.quit()
        log.info(f"Alert sent: {message}")
    except smtplib.SMTPAuthenticationError as e:
        log.error(f"Failed to send alert: SMTP authentication failed: {e}")
        log.warning('Check ALERT_EMAIL_USER and ALERT_EMAIL_PASS, and if using Gmail enable an App Password.')
    except Exception as e:
        log.error(f"Failed to send alert: {e}")
