"""Second-layer email discovery — crawl website contact pages."""

import logging
import re

import requests

log = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
CONTACT_PATHS = ["/contact", "/contact-us", "/about", "/about-us", "/team"]
FAKE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".css", ".js", ".webp")
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; TeLaLiveBot/1.0)"}


def _clean_emails(raw: list[str]) -> list[str]:
    """Filter out image/asset false positives and common no-reply addresses."""
    seen = set()
    clean = []
    for e in raw:
        e = e.lower()
        if e in seen:
            continue
        if any(e.endswith(s) for s in FAKE_SUFFIXES):
            continue
        if e.startswith(("noreply@", "no-reply@", "mailer-daemon@")):
            continue
        seen.add(e)
        clean.append(e)
    return clean


def find_email(website: str) -> str | None:
    """Try to find a contact email by crawling the website's contact pages.

    Returns the first valid email found, or None.
    """
    if not website:
        return None

    base = website.rstrip("/")
    if not base.startswith(("http://", "https://")):
        base = "https://" + base

    # Try contact sub-pages first, then homepage as fallback
    paths_to_try = CONTACT_PATHS + [""]
    for path in paths_to_try:
        url = base + path
        try:
            resp = requests.get(url, timeout=10, headers=HEADERS, allow_redirects=True)
            if not resp.ok:
                continue
            emails = EMAIL_RE.findall(resp.text)
            clean = _clean_emails(emails)
            if clean:
                log.info("Found email %s on %s", clean[0], url)
                return clean[0]
        except Exception:
            continue

    return None
