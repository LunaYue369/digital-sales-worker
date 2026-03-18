import base64
import json
import logging
import os
import random
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


# Human-like send delay: normal distribution with occasional pauses
# Target: ~20 emails in ~10 min → ~30s average
SEND_DELAY_MEAN = float(os.getenv("SEND_DELAY_MEAN", "25"))      # 25s average
SEND_DELAY_STDDEV = float(os.getenv("SEND_DELAY_STDDEV", "10"))  # 10s std dev
SEND_DELAY_MIN = float(os.getenv("SEND_DELAY_MIN", "8"))         # floor: 8s
SEND_DELAY_MAX = float(os.getenv("SEND_DELAY_MAX", "60"))        # cap: 1 min
SEND_DELAY_BREAK_CHANCE = float(os.getenv("SEND_DELAY_BREAK_CHANCE", "0.05"))  # 5% chance of pause
SEND_DELAY_BREAK_MIN = float(os.getenv("SEND_DELAY_BREAK_MIN", "60"))    # pause: 1-2 min
SEND_DELAY_BREAK_MAX = float(os.getenv("SEND_DELAY_BREAK_MAX", "120"))


def _human_delay() -> int:
    """Generate a human-like delay between emails.

    Most delays cluster around 2-4 min (normal distribution).
    ~10% chance of a longer "break" (5-15 min) simulating distractions.
    """
    if random.random() < SEND_DELAY_BREAK_CHANCE:
        # Long pause — coffee, bathroom, Slack distraction
        delay = random.uniform(SEND_DELAY_BREAK_MIN, SEND_DELAY_BREAK_MAX)
    else:
        # Normal send — writing/reviewing an email
        delay = random.gauss(SEND_DELAY_MEAN, SEND_DELAY_STDDEV)
        delay = max(SEND_DELAY_MIN, min(delay, SEND_DELAY_MAX))
    return int(delay)


def send_campaign(user_id: str, emails: list[dict], campaign_id: str) -> dict:
    sent_count = 0
    failed_count = 0

    with _lock:
        sent_log = _load_sent_log(user_id)

        for i, email in enumerate(emails):
            # Human-like delay between emails
            if i > 0:
                delay = _human_delay()
                log.info("Waiting %ds before next email... (%.1f min)", delay, delay / 60)
                time.sleep(delay)

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
