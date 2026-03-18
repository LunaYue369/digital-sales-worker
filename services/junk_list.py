"""Shared junk email/domain list — learns from bounces, shared across all users.

File: data/junk_learned.json
Structure:
{
  "emails": {"bad@fake.com": {"source": "bounce", "added": "2026-03-18", "user": "nate"}},
  "domains": {"deadcompany.com": {"source": "bounce", "count": 3, "added": "2026-03-18"}}
}

Hardcoded rules live in email_finder.py (PLACEHOLDER_EMAILS, JUNK_DOMAINS, etc.)
This module handles ONLY the learned/dynamic rules from bounce feedback.
"""

import json
import logging
import os
import threading
from datetime import datetime

log = logging.getLogger(__name__)

_lock = threading.Lock()
_JUNK_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "junk_learned.json")
_JUNK_PATH = os.path.normpath(_JUNK_PATH)

# In-memory cache — loaded once, updated on writes
_cache: dict | None = None


def _load() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    if os.path.exists(_JUNK_PATH):
        try:
            with open(_JUNK_PATH, "r", encoding="utf-8") as f:
                _cache = json.load(f)
        except (json.JSONDecodeError, OSError):
            _cache = {"emails": {}, "domains": {}}
    else:
        _cache = {"emails": {}, "domains": {}}
    return _cache


def _save(data: dict):
    global _cache
    _cache = data
    os.makedirs(os.path.dirname(_JUNK_PATH), exist_ok=True)
    with open(_JUNK_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def is_learned_junk(email: str) -> bool:
    """Check if email or its domain is in the learned junk list."""
    e = email.lower().strip()
    with _lock:
        data = _load()
    if e in data["emails"]:
        return True
    _, _, domain = e.partition("@")
    if domain in data["domains"]:
        return True
    return False


def report_bounce(email: str, user_name: str = ""):
    """Record a bounced email. If a domain accumulates 3+ bounces, auto-block it."""
    e = email.lower().strip()
    if not e or "@" not in e:
        return
    _, _, domain = e.partition("@")
    today = datetime.now().strftime("%Y-%m-%d")

    with _lock:
        data = _load()

        # Add email to junk list
        if e not in data["emails"]:
            data["emails"][e] = {
                "source": "bounce",
                "added": today,
                "user": user_name,
            }
            log.info("Junk list: added bounced email %s", e)

        # Track domain bounce count
        domain_entry = data.setdefault("domains_pending", {}).get(domain)
        if domain_entry:
            domain_entry["count"] += 1
            domain_entry["last_bounce"] = today
        else:
            data.setdefault("domains_pending", {})[domain] = {
                "count": 1,
                "first_bounce": today,
                "last_bounce": today,
            }

        # Auto-promote domain to blocked if 3+ distinct emails bounced
        pending = data.get("domains_pending", {}).get(domain, {})
        if pending.get("count", 0) >= 3 and domain not in data["domains"]:
            data["domains"][domain] = {
                "source": "bounce_auto",
                "count": pending["count"],
                "added": today,
            }
            log.warning("Junk list: auto-blocked domain %s (%d bounces)", domain, pending["count"])

        _save(data)


def get_stats() -> dict:
    """Return summary stats for admin reporting."""
    with _lock:
        data = _load()
    return {
        "blocked_emails": len(data.get("emails", {})),
        "blocked_domains": len(data.get("domains", {})),
        "pending_domains": len(data.get("domains_pending", {})),
    }
