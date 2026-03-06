"""Send emails via Gmail API."""

import base64
import json
import logging
import os
import threading
import time
from email.mime.text import MIMEText

from services.auth import get_gmail_service

log = logging.getLogger(__name__)

SENT_LOG_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "sent_log.json")
_lock = threading.Lock()


def _load_sent_log() -> list[dict]:
    if os.path.exists(SENT_LOG_PATH):
        with open(SENT_LOG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def _save_sent_log(data: list[dict]):
    os.makedirs(os.path.dirname(SENT_LOG_PATH), exist_ok=True)
    with open(SENT_LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def send_email(to: str, subject: str, body: str) -> bool:
    """Send a single email via Gmail API. Returns True on success."""
    try:
        gmail = get_gmail_service()
        msg = MIMEText(body, "plain", "utf-8")
        msg["to"] = to
        msg["subject"] = subject
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        gmail.users().messages().send(
            userId="me", body={"raw": raw}
        ).execute()
        log.info("Email sent to %s", to)
        return True
    except Exception as e:
        log.error("Failed to send email to %s: %s", to, e)
        return False


def send_campaign(emails: list[dict], campaign_id: str) -> dict:
    """
    Send a batch of emails and log results.
    Returns: {sent: int, failed: int}
    """
    sent_count = 0
    failed_count = 0

    with _lock:
        sent_log = _load_sent_log()

        for email in emails:
            success = send_email(email["contact_email"], email["subject"], email["body"])

            record = {
                "campaign_id": campaign_id,
                "company_name": email.get("company_name", ""),
                "contact_email": email["contact_email"],
                "subject": email["subject"],
                "status": "sent" if success else "failed",
                "sent_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            sent_log.append(record)

            if success:
                sent_count += 1
            else:
                failed_count += 1

        _save_sent_log(sent_log)

    return {"sent": sent_count, "failed": failed_count}


def get_sent_count() -> int:
    with _lock:
        return len(_load_sent_log())


def get_sent_log() -> list[dict]:
    with _lock:
        return _load_sent_log()
