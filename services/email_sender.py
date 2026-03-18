import base64
import json
import logging
import os
import threading
import time
from email.mime.text import MIMEText

from services.auth import get_gmail_service
from core.user_config import user_data_dir

log = logging.getLogger(__name__)

_lock = threading.Lock()


def _sent_log_path(user_id: str) -> str:
    return os.path.join(user_data_dir(user_id), "sent_log.json")


def _load_sent_log(user_id: str) -> list[dict]:
    path = _sent_log_path(user_id)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def _save_sent_log(user_id: str, data: list[dict]):
    path = _sent_log_path(user_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def send_email(user_id: str, to: str, subject: str, body: str) -> dict | None:
    """Send a single email via Gmail API. Returns Gmail message dict on success, None on failure."""
    try:
        gmail = get_gmail_service(user_id)
        msg = MIMEText(body, "plain", "utf-8")
        msg["to"] = to
        msg["subject"] = subject
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        result = gmail.users().messages().send(
            userId="me", body={"raw": raw}
        ).execute()
        log.info("Email sent to %s (threadId=%s)", to, result.get("threadId"))
        return result
    except Exception as e:
        log.error("Failed to send email to %s: %s", to, e)
        return None


def send_campaign(user_id: str, emails: list[dict], campaign_id: str) -> dict:
    sent_count = 0
    failed_count = 0

    with _lock:
        sent_log = _load_sent_log(user_id)

        for email in emails:
            result = send_email(user_id, email["contact_email"], email["subject"], email["body"])
            success = result is not None
            record = {
                "campaign_id": campaign_id,
                "company_name": email.get("company_name", ""),
                "contact_email": email["contact_email"],
                "subject": email["subject"],
                "body": email.get("body", ""),
                "industry": email.get("industry", ""),
                "thread_id": result.get("threadId", "") if result else "",
                "message_id": result.get("id", "") if result else "",
                "status": "sent" if success else "failed",
                "sent_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            sent_log.append(record)
            _save_sent_log(user_id, sent_log)
            if success:
                sent_count += 1
            else:
                failed_count += 1

    return {"sent": sent_count, "failed": failed_count}


def get_sent_count(user_id: str) -> int:
    with _lock:
        return len(_load_sent_log(user_id))


def get_sent_log(user_id: str) -> list[dict]:
    with _lock:
        return _load_sent_log(user_id)
