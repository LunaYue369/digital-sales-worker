"""Singleton Firestore client.

Auth resolution order (handled by google-cloud SDK automatically):
  1. GOOGLE_APPLICATION_CREDENTIALS env var pointing at a service account JSON
     (production deploy path — currently blocked by org policy
     `iam.disableServiceAccountKeyCreation`, will need either policy lift or
     Workload Identity Federation before prod)
  2. Application Default Credentials from `gcloud auth application-default login`
     (local development path — what we use now)

Use `get_db()` to fetch the client. First call constructs it; subsequent calls
return the same instance. Thread-safe (Firestore client is concurrent-safe).

Schema layout: see firestore_schema.md at the repo root.
"""

import logging
import os
import threading

from google.cloud import firestore

log = logging.getLogger(__name__)

PROJECT_ID = os.getenv("GCP_PROJECT_ID")
if not PROJECT_ID:
    raise RuntimeError("GCP_PROJECT_ID env var must be set before using Firestore")
SA_KEY_PATH = os.getenv("FIRESTORE_SA_KEY", "firebase-sa.json")

_client: firestore.Client | None = None
_lock = threading.Lock()


def get_db() -> firestore.Client:
    """Return the singleton Firestore client. Initializes on first call."""
    global _client
    if _client is not None:
        return _client
    with _lock:
        if _client is not None:
            return _client
        # Surface the SA key only if it actually exists; otherwise let ADC handle it.
        if os.path.exists(SA_KEY_PATH) and not os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = SA_KEY_PATH
            log.info("Firestore auth: service account key %s", SA_KEY_PATH)
        else:
            log.info("Firestore auth: Application Default Credentials")
        _client = firestore.Client(project=PROJECT_ID)
        log.info("Firestore client initialized for project %s", PROJECT_ID)
        return _client


# ── Collection path helpers ──────────────────────────────────────────────
# Centralized so collection paths aren't sprinkled as raw strings across
# the codebase. Refactor-safe: rename here, all callers follow.

def prospects_col():
    return get_db().collection("prospects")


def research_cache_col():
    return get_db().collection("research_cache")


def user_emails_col(user_id: str):
    return get_db().collection("users").document(user_id).collection("emails")


def user_usage_col(user_id: str):
    return get_db().collection("users").document(user_id).collection("usage")


def user_replies_col(user_id: str):
    return get_db().collection("users").document(user_id).collection("replies")


def user_processed_files_col(user_id: str):
    return get_db().collection("users").document(user_id).collection("processed_files")
