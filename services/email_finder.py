"""Second-layer email discovery — crawl website contact pages.

Multi-strategy extraction with scoring:
1. mailto: links      — strongest signal (explicit contact intent)
2. JSON-LD schema     — structured data ("email" field in LD+JSON)
3. Visible text scan  — regex on text content (script/style stripped)
4. Full HTML fallback — regex on raw HTML (lowest confidence)

Each candidate is scored and the best one is returned.
MX record check is used as a final gate to avoid sending to dead domains.
"""

import dns.resolver
import json
import logging
import re
from functools import lru_cache
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from services.junk_list import is_learned_junk

log = logging.getLogger(__name__)

# ── Regex ─────────────────────────────────────────────────────────────
# RFC 5321 simplified — intentionally strict to reduce false positives
EMAIL_RE = re.compile(
    r"[a-zA-Z0-9](?:[a-zA-Z0-9._%+\-]{0,62}[a-zA-Z0-9])?@"
    r"[a-zA-Z0-9](?:[a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?"
    r"(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?)*"
    r"\.[a-zA-Z]{2,}"
)
MAILTO_RE = re.compile(r'mailto:([^"\'?\s&]+)', re.IGNORECASE)

# ── Contact page paths (ordered by likelihood) ───────────────────────
CONTACT_PATHS = [
    "/contact", "/contact-us", "/about", "/about-us",
    "/team", "/about/contact", "/company/contact",
    "/get-in-touch", "/reach-us", "/support",
]

# ── Filters ───────────────────────────────────────────────────────────
# File extensions that look like emails but aren't (e.g. icon@2x.png)
FAKE_SUFFIXES = (
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".css", ".js", ".webp",
    ".woff", ".woff2", ".ttf", ".eot", ".ico", ".map", ".pdf",
)

# Known placeholder / dummy addresses
PLACEHOLDER_EMAILS = {
    "user@domain.com", "email@domain.com", "info@domain.com",
    "contact@domain.com", "admin@domain.com", "user@example.com",
    "email@example.com", "info@example.com", "test@example.com",
    "your@email.com", "name@domain.com", "example@example.com",
    "someone@example.com", "youremail@domain.com", "mail@example.com",
    "username@domain.com", "yourname@email.com",
}

# Technical / infrastructure domains — never real contact addresses
JUNK_DOMAINS = {
    # Error tracking & analytics
    "sentry.io", "wixpress.com", "sentry-next.wixpress.com",
    "bugsnag.com", "rollbar.com", "loggly.com", "newrelic.com",
    # Web infrastructure
    "w3.org", "schema.org", "json-ld.org", "microformats.org",
    "googleusercontent.com", "googleapis.com", "gstatic.com",
    "cloudflare.com", "cloudflare-dns.com", "cdnjs.cloudflare.com",
    "amazonaws.com", "azurewebsites.net", "herokuapp.com",
    # CMS / website builders
    "wordpress.org", "wordpress.com", "squarespace.com",
    "wix.com", "weebly.com", "shopify.com", "godaddy.com",
    # Social / no-reply
    "facebook.com", "twitter.com", "instagram.com", "linkedin.com",
    "youtube.com", "tiktok.com", "pinterest.com",
    "google.com", "apple.com", "microsoft.com",
    # Email infrastructure
    "mailchimp.com", "sendgrid.net", "mailgun.org", "constantcontact.com",
    "campaign-archive.com", "list-manage.com",
}

# Local parts that are never useful for cold outreach
JUNK_LOCAL_PREFIXES = (
    "noreply", "no-reply", "no_reply", "donotreply", "do-not-reply",
    "mailer-daemon", "postmaster", "hostmaster", "webmaster",
    "abuse", "unsubscribe", "bounce", "auto-reply",
)

# Hex hash pattern — Sentry DSNs, tracking IDs, etc.
HEX_HASH_RE = re.compile(r"^[0-9a-f]{16,}$")

# Local part that's clearly not human-readable
GARBAGE_LOCAL_RE = re.compile(r"^[0-9a-f\-]{30,}$")

# ── Scoring: preferred contact prefixes ───────────────────────────────
# Higher score = more likely to be the right contact email
CONTACT_PREFIXES = {
    "info": 10, "contact": 10, "hello": 9, "hi": 9,
    "inquiries": 8, "inquiry": 8, "enquiries": 8,
    "sales": 7, "support": 6, "help": 6,
    "admin": 5, "office": 5, "general": 5,
    "reception": 5, "frontdesk": 5, "appointments": 5,
    "reservations": 5, "booking": 5, "orders": 5,
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

MX_CACHE_SIZE = 512


# ══════════════════════════════════════════════════════════════════════
# Public API
# ══════════════════════════════════════════════════════════════════════

def is_junk_email(email: str) -> bool:
    """Check if an email is a placeholder, tracking hash, or other junk.

    Two layers:
    1. Hardcoded rules (PLACEHOLDER_EMAILS, JUNK_DOMAINS, etc.)
    2. Learned rules from bounce feedback (data/junk_learned.json)
    """
    e = email.lower().strip()
    if not e or "@" not in e:
        return True
    if e in PLACEHOLDER_EMAILS:
        return True
    local, _, domain = e.partition("@")
    if _is_junk_domain(domain):
        return True
    if _is_junk_local(local):
        return True
    if any(e.endswith(s) for s in FAKE_SUFFIXES):
        return True
    if is_learned_junk(e):
        return True
    return False


def find_email(website: str) -> str | None:
    """Find a contact email by crawling the website.

    Multi-strategy: mailto > JSON-LD > visible text > raw HTML.
    Returns the best candidate that passes validation, or None.
    """
    if not website:
        return None

    base = website.rstrip("/")
    if not base.startswith(("http://", "https://")):
        base = "https://" + base

    site_domain = _extract_domain(base)

    # Try contact sub-pages first, then homepage as fallback
    paths_to_try = CONTACT_PATHS + [""]
    for path in paths_to_try:
        url = base + path
        try:
            resp = requests.get(url, timeout=10, headers=HEADERS, allow_redirects=True)
            if not resp.ok:
                continue

            best = _extract_best_email(resp.text, site_domain)
            if best:
                log.info("Found email %s on %s", best, url)
                return best

        except Exception:
            continue

    return None


# ══════════════════════════════════════════════════════════════════════
# Extraction strategies (ordered by confidence)
# ══════════════════════════════════════════════════════════════════════

def _extract_best_email(html: str, site_domain: str) -> str | None:
    """Run all extraction strategies, score candidates, return the best."""
    candidates: dict[str, int] = {}  # email → score

    soup = BeautifulSoup(html, "html.parser")

    # Strategy 1: mailto: links (highest confidence)
    _extract_mailto(html, candidates, base_score=50)

    # Strategy 2: JSON-LD structured data
    _extract_jsonld(soup, candidates, base_score=40)

    # Strategy 3: Visible text only (script/style stripped)
    _extract_visible_text(soup, candidates, base_score=20)

    # Strategy 4: Raw HTML fallback (lowest confidence)
    _extract_raw_html(html, candidates, base_score=10)

    if not candidates:
        return None

    # Filter junk, then pick the best
    valid = {e: s for e, s in candidates.items() if not is_junk_email(e)}
    if not valid:
        return None

    # Boost score for contact-like prefixes
    scored = {}
    for email, score in valid.items():
        local = email.split("@")[0]
        prefix_bonus = CONTACT_PREFIXES.get(local, 0)
        # Boost emails whose domain matches the website
        domain = email.split("@")[1]
        domain_bonus = 15 if _domains_match(domain, site_domain) else 0
        scored[email] = score + prefix_bonus + domain_bonus

    # Sort by score descending, pick the best
    best_email = max(scored, key=scored.get)

    # Final gate: MX record check
    domain = best_email.split("@")[1]
    if not _has_mx_record(domain):
        log.debug("No MX record for %s, skipping %s", domain, best_email)
        # Try next best candidates
        for email in sorted(scored, key=scored.get, reverse=True)[1:]:
            d = email.split("@")[1]
            if _has_mx_record(d):
                return email
        return None

    return best_email


def _extract_mailto(html: str, candidates: dict, base_score: int):
    """Extract emails from mailto: links."""
    for match in MAILTO_RE.finditer(html):
        raw = match.group(1).strip()
        # mailto: can have ?subject=... — take only the address part
        email = raw.split("?")[0].lower().strip()
        if EMAIL_RE.fullmatch(email):
            candidates[email] = max(candidates.get(email, 0), base_score)


def _extract_jsonld(soup: BeautifulSoup, candidates: dict, base_score: int):
    """Extract emails from JSON-LD structured data (schema.org)."""
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            _walk_jsonld(data, candidates, base_score)
        except (json.JSONDecodeError, TypeError):
            continue


def _walk_jsonld(data, candidates: dict, base_score: int):
    """Recursively walk JSON-LD and extract email fields."""
    if isinstance(data, dict):
        for key in ("email", "contactPoint", "author", "member"):
            if key in data:
                val = data[key]
                if isinstance(val, str):
                    email = val.lower().replace("mailto:", "").strip()
                    if EMAIL_RE.fullmatch(email):
                        candidates[email] = max(candidates.get(email, 0), base_score)
                elif isinstance(val, (dict, list)):
                    _walk_jsonld(val, candidates, base_score)
        for v in data.values():
            if isinstance(v, (dict, list)):
                _walk_jsonld(v, candidates, base_score)
    elif isinstance(data, list):
        for item in data:
            _walk_jsonld(item, candidates, base_score)


def _extract_visible_text(soup: BeautifulSoup, candidates: dict, base_score: int):
    """Extract emails from visible page text (script/style/head stripped)."""
    # Remove non-visible elements
    for tag in soup.find_all(["script", "style", "noscript", "head", "meta", "link"]):
        tag.decompose()

    text = soup.get_text(separator=" ")
    for match in EMAIL_RE.finditer(text):
        email = match.group(0).lower()
        candidates[email] = max(candidates.get(email, 0), base_score)


def _extract_raw_html(html: str, candidates: dict, base_score: int):
    """Last resort: regex over raw HTML. Low confidence."""
    for match in EMAIL_RE.finditer(html):
        email = match.group(0).lower()
        if email not in candidates:  # don't overwrite higher-score entries
            candidates[email] = base_score


# ══════════════════════════════════════════════════════════════════════
# Validation helpers
# ══════════════════════════════════════════════════════════════════════

def _is_junk_domain(domain: str) -> bool:
    """Check if domain is a known technical/infrastructure domain."""
    domain = domain.lower()
    for junk in JUNK_DOMAINS:
        if domain == junk or domain.endswith("." + junk):
            return True
    return False


def _is_junk_local(local: str) -> bool:
    """Check if the local part is junk (hash, noreply, etc.)."""
    local = local.lower()
    if any(local.startswith(p) for p in JUNK_LOCAL_PREFIXES):
        return True
    if HEX_HASH_RE.match(local):
        return True
    if GARBAGE_LOCAL_RE.match(local):
        return True
    # Too short (single char) or too long (>64 per RFC)
    if len(local) < 2 or len(local) > 64:
        return True
    return False


@lru_cache(maxsize=MX_CACHE_SIZE)
def _has_mx_record(domain: str) -> bool:
    """Check if domain has MX records (can receive email). Cached."""
    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=5)
        return len(answers) > 0
    except dns.resolver.NXDOMAIN:
        # Domain doesn't exist — definitely can't receive email
        return False
    except (dns.resolver.NoAnswer, dns.resolver.NoNameservers,
            dns.exception.Timeout, dns.resolver.LifetimeTimeout):
        # DNS issue but domain might exist — be permissive
        return True
    except Exception:
        return True


def _extract_domain(url: str) -> str:
    """Extract domain from URL."""
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        return host.lower().removeprefix("www.")
    except Exception:
        return ""


def _domains_match(email_domain: str, site_domain: str) -> bool:
    """Check if email domain plausibly belongs to the website."""
    ed = email_domain.lower().removeprefix("www.")
    sd = site_domain.lower().removeprefix("www.")
    # Exact match or one is a subdomain of the other
    return ed == sd or ed.endswith("." + sd) or sd.endswith("." + ed)
