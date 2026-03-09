"""Failed email log — records pipeline errors and review-discarded emails."""

import json
import logging
import os
import threading
import time

log = logging.getLogger(__name__)

FAILED_LOG_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "failed_log.json")
_lock = threading.Lock()


def _load() -> list[dict]:
    if os.path.exists(FAILED_LOG_PATH):
        with open(FAILED_LOG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def _save(data: list[dict]):
    os.makedirs(os.path.dirname(FAILED_LOG_PATH), exist_ok=True)
    with open(FAILED_LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def record_error(company: dict, campaign_id: str, error: str):
    """Record a company that failed due to API/network errors after all retries."""
    entry = {
        "failure_type": "error",
        "campaign_id": campaign_id,
        "company_name": company.get("company_name", "?"),
        "industry": company.get("industry", ""),
        "contact_email": company.get("contact_email", ""),
        "error": error,
        "failed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    with _lock:
        data = _load()
        data.append(entry)
        _save(data)


def record_discarded(company: dict, campaign_id: str):
    """Record a company whose email was discarded after failing all review rounds."""
    entry = {
        "failure_type": "discarded",
        "campaign_id": campaign_id,
        "company_name": company.get("company_name", "?"),
        "industry": company.get("industry", ""),
        "contact_email": company.get("contact_email", ""),
        "subject": company.get("subject", ""),
        "failed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    with _lock:
        data = _load()
        data.append(entry)
        _save(data)


def get_failed_log() -> list[dict]:
    with _lock:
        return _load()
