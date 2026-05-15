import base64
import logging
import os
import random
import time
from datetime import datetime, timezone
from email.mime.text import MIMEText

from services.auth import get_gmail_service
from services.firestore_client import user_emails_col
from core import state

log = logging.getLogger(__name__)


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


def _compact_now() -> str:
    """UTC timestamp in 'YYYYMMDD_HHMMSS' for synthesizing doc ids on send-failed rows."""
    return datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")


def send_campaign(user_id: str, emails: list[dict], campaign_id: str) -> dict:
    """Send each email one-by-one. On each result, write a doc to
    users/{uid}/emails. Successful sends use the Gmail message id as the doc id
    (globally unique). Sends that fail at the Gmail-API level get a synthesized
    doc id since there is no message id to anchor on."""
    sent_count = 0
    failed_count = 0
    col = user_emails_col(user_id)

    for i, email in enumerate(emails):
        # Honor `stop auto` between emails — the in-flight Gmail API call
        # is atomic, but we never start the next one once stop is signaled.
        if not state.is_auto_running(user_id):
            log.info("Stop signal received — halting send at %d sent / %d failed (of %d queued)",
                     sent_count, failed_count, len(emails))
            break

        # Human-like delay between emails
        if i > 0:
            delay = _human_delay()
            log.info("Waiting %ds before next email... (%.1f min)", delay, delay / 60)
            time.sleep(delay)

        result = send_email(user_id, email["contact_email"], email["subject"], email["body"])
        success = result is not None
        now = datetime.now(tz=timezone.utc)

        if success:
            doc_id = result["id"]
            doc = {
                "campaign_id": campaign_id,
                "user_id": user_id,
                "template": email.get("template"),
                "company_name": email.get("company_name", ""),
                "contact_email": email["contact_email"],
                "industry": email.get("industry", ""),
                "source_drive_file_id": email.get("source_drive_file_id"),
                "source_csv_filename": email.get("source_csv_filename"),
                "subject": email["subject"],
                "body": email.get("body", ""),
                "status": "sent",
                "created_at": now,
                "approved_at": now,
                "approved_by": email.get("approved_by"),
                "sent_at": now,
                "reviewer_rounds": email.get("reviewer_rounds"),
                "reviewer_scores": email.get("reviewer_scores"),
                "reviewer_verdict": email.get("reviewer_verdict"),
                "rejected_reason": None,
                "error": None,
                "gmail_thread_id": result.get("threadId"),
                "gmail_message_id": result["id"],
            }
            sent_count += 1
        else:
            doc_id = f"sendfailed_{email['contact_email']}_{_compact_now()}"
            doc = {
                "campaign_id": campaign_id,
                "user_id": user_id,
                "template": email.get("template"),
                "company_name": email.get("company_name", ""),
                "contact_email": email["contact_email"],
                "industry": email.get("industry", ""),
                "source_drive_file_id": email.get("source_drive_file_id"),
                "source_csv_filename": email.get("source_csv_filename"),
                "subject": email["subject"],
                "body": email.get("body", ""),
                "status": "failed",
                "created_at": now,
                "approved_at": now,
                "approved_by": email.get("approved_by"),
                "sent_at": None,
                "reviewer_rounds": email.get("reviewer_rounds"),
                "reviewer_scores": email.get("reviewer_scores"),
                "reviewer_verdict": email.get("reviewer_verdict"),
                "rejected_reason": None,
                "error": "Gmail API send failed",
                "gmail_thread_id": None,
                "gmail_message_id": None,
            }
            failed_count += 1

        col.document(doc_id).set(doc)

    return {"sent": sent_count, "failed": failed_count}


def get_sent_count(user_id: str) -> int:
    """Total emails this user has successfully sent (status=sent)."""
    return sum(1 for _ in user_emails_col(user_id).where("status", "==", "sent").stream())


def get_sent_log(user_id: str) -> list[dict]:
    """Return all sent emails for this user as a list of dicts, ordered by sent_at asc.
    Caller-friendly: same dict shape as the Firestore docs."""
    docs = user_emails_col(user_id).where("status", "==", "sent").stream()
    return sorted(
        (doc.to_dict() for doc in docs),
        key=lambda d: d.get("sent_at") or datetime.min.replace(tzinfo=timezone.utc),
    )
