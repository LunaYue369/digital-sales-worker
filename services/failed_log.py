"""Failure recording — per-user. Writes pipeline errors and review-discarded
emails into the users/{uid}/emails Firestore collection, using the unified
status state machine from firestore_schema.md."""

import logging
from datetime import datetime, timezone

from services.firestore_client import user_emails_col

log = logging.getLogger(__name__)


def _compact_now() -> str:
    """UTC 'YYYYMMDD_HHMMSS' for doc id synthesis (no Gmail message id available
    when the email never got sent)."""
    return datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")


def _base_doc(user_id: str, company: dict, campaign_id: str, now: datetime) -> dict:
    """Common fields shared by every recorded failure / discard."""
    return {
        "campaign_id": campaign_id,
        "user_id": user_id,
        "template": company.get("template"),
        "company_name": company.get("company_name", "?"),
        "contact_email": company.get("contact_email", ""),
        "industry": company.get("industry", ""),
        "source_drive_file_id": company.get("source_drive_file_id"),
        "source_csv_filename": company.get("source_csv_filename"),
        "subject": company.get("subject"),
        "body": None,
        "created_at": now,
        "approved_at": None,
        "approved_by": None,
        "sent_at": None,
        "reviewer_rounds": company.get("reviewer_rounds"),
        "reviewer_scores": company.get("reviewer_scores"),
        "reviewer_verdict": company.get("reviewer_verdict"),
        "gmail_thread_id": None,
        "gmail_message_id": None,
    }


def record_error(user_id: str, company: dict, campaign_id: str, error: str):
    """Record a pipeline-level error (research / write / review / retry exhausted)."""
    now = datetime.now(tz=timezone.utc)
    contact = company.get("contact_email", "noemail")
    doc_id = f"failed_{contact}_{_compact_now()}"
    doc = {
        **_base_doc(user_id, company, campaign_id, now),
        "status": "failed",
        "rejected_reason": None,
        "error": error,
    }
    user_emails_col(user_id).document(doc_id).set(doc)


def record_discarded(user_id: str, company: dict, campaign_id: str):
    """Record a GPT Reviewer 3-round failure (the email was written but rejected
    every revision pass and won't be sent)."""
    now = datetime.now(tz=timezone.utc)
    contact = company.get("contact_email", "noemail")
    doc_id = f"discarded_{contact}_{_compact_now()}"
    doc = {
        **_base_doc(user_id, company, campaign_id, now),
        "status": "rejected_by_reviewer",
        "rejected_reason": "GPT Reviewer 3-round failure",
        "error": None,
    }
    user_emails_col(user_id).document(doc_id).set(doc)


def get_failed_log(user_id: str) -> list[dict]:
    """All failure-flavored docs for this user (failed + rejected_by_reviewer +
    rejected_by_human), ordered by created_at asc."""
    docs = user_emails_col(user_id).where(
        "status", "in", ["failed", "rejected_by_reviewer", "rejected_by_human"]
    ).stream()
    return sorted(
        (doc.to_dict() for doc in docs),
        key=lambda d: d.get("created_at") or datetime.min.replace(tzinfo=timezone.utc),
    )
