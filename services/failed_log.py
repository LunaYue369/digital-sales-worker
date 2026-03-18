"""Failed email log — per-user, records pipeline errors and review-discarded emails."""

import json
import logging
import os
import threading
import time

from core.user_config import user_data_dir

log = logging.getLogger(__name__)

_lock = threading.Lock()


def _failed_log_path(user_id: str) -> str:
    return os.path.join(user_data_dir(user_id), "failed_log.json")


def _load(user_id: str) -> list[dict]:
    path = _failed_log_path(user_id)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def _save(user_id: str, data: list[dict]):
    path = _failed_log_path(user_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def record_error(user_id: str, company: dict, campaign_id: str, error: str):
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
        data = _load(user_id)
        data.append(entry)
        _save(user_id, data)


def record_discarded(user_id: str, company: dict, campaign_id: str):
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
        data = _load(user_id)
        data.append(entry)
        _save(user_id, data)


def get_failed_log(user_id: str) -> list[dict]:
    with _lock:
        return _load(user_id)
