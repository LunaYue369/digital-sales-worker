"""Prospect pipeline — per-user. Find leads via Google Maps scraper, enrich emails, upload to Drive.

Only generates CSV and uploads to Drive. Does NOT send emails.
"""

import csv
import json
import logging
import os
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import unquote

from services import drive_uploader
from services.email_finder import find_email, is_junk_email, extract_domain
from services.firestore_client import prospects_col
from services.spreadsheet import _load_sent_emails
from core.user_config import user_data_dir, DATA_DIR

log = logging.getLogger(__name__)

SCRAPER_PATH = os.getenv("GMAPS_SCRAPER_PATH", "gmaps-scraper.exe")
SCRAPER_DEPTH = int(os.getenv("PROSPECT_SCRAPER_DEPTH", "1"))
SCRAPER_TIMEOUT = int(os.getenv("PROSPECT_SCRAPER_TIMEOUT", "1800"))
EMAIL_FINDER_WORKERS = int(os.getenv("PROSPECT_EMAIL_WORKERS", "5"))

# Global concurrency cap on the gmaps scraper subprocess. Multiple sales running
# `prospect` at once from the same IP will trigger Google's anti-bot defenses
# (CAPTCHA, IP ban). 2 is a safe default; tune via env var if proxies are added.
PROSPECT_MAX_CONCURRENT = int(os.getenv("PROSPECT_MAX_CONCURRENT", "2"))
_scraper_semaphore = threading.Semaphore(PROSPECT_MAX_CONCURRENT)

OUTPUT_COLUMNS = [
    "company_name", "contact_email", "website", "industry",
    "city", "state", "country", "phone",
    "review_rating", "review_count",
]


def _prospect_dir(user_id: str) -> str:
    return os.path.join(user_data_dir(user_id), "prospect_results")


def run_prospect(user_id: str, queries: list[str], say, depth: int | None = None, debug: bool = False, template: str | None = None):
    """One-shot: run scraper → find emails → upload CSV to Drive.

    If `template` is provided, the output filename is tagged with `__<template>`
    so the auto pipeline can route this CSV to that specific template.
    """
    say(f"Prospect started with {len(queries)} search queries:\n"
        + "\n".join(f"  • {q}" for q in queries))
    if template:
        say(f"Output CSV will be tagged for template: `{template}`")

    # 1. Run gosom scraper — globally rate-limited to avoid Google anti-bot
    effective_depth = depth or SCRAPER_DEPTH
    mode = "headful (visible browser)" if debug else "headless"

    if not _scraper_semaphore.acquire(blocking=False):
        say(f":hourglass: Scraper is at max concurrency ({PROSPECT_MAX_CONCURRENT} running). "
            f"Queued — will start as soon as a slot opens.")
        _scraper_semaphore.acquire()
        say(":white_check_mark: Slot acquired, starting scraper now.")

    try:
        say(f"Running Google Maps scraper (depth={effective_depth}, {mode}, this may take a few minutes)...")
        raw_csv = _run_scraper(user_id, queries, effective_depth, debug=debug)
    finally:
        _scraper_semaphore.release()

    if not raw_csv:
        say("Scraper returned no results.")
        return

    # 2. Parse raw CSV from gosom
    raw_leads = _parse_gosom_csv(raw_csv)
    say(f"Scraper found {len(raw_leads)} businesses.")

    if not raw_leads:
        return

    # 3. Dedup against sent_log + prospect_log
    sent_emails = _load_sent_emails(user_id)
    sent_domains = set()
    for e in sent_emails:
        parts = e.split("@")
        if len(parts) == 2:
            sent_domains.add(parts[1].lower())

    prospect_log = _load_prospect_log()

    leads = []
    skipped_sent = 0
    skipped_prospect = 0
    for l in raw_leads:
        domain = extract_domain(l["website"])
        if domain in sent_domains or l["contact_email"].lower() in sent_emails:
            skipped_sent += 1
        elif _lead_is_prospected(l, prospect_log):
            skipped_prospect += 1
        else:
            leads.append(l)

    if skipped_sent:
        say(f"Skipped {skipped_sent} already-contacted companies.")
    if skipped_prospect:
        say(f"Skipped {skipped_prospect} previously-prospected companies.")

    if not leads:
        say("No new leads after dedup.")
        return

    # 4. Find missing emails
    missing = [l for l in leads if not l["contact_email"]]
    has_email = len(leads) - len(missing)
    if missing:
        say(f"{has_email} have emails from scraper. Searching websites for {len(missing)} missing emails...")
        _enrich_emails(missing)
        found = sum(1 for l in missing if l["contact_email"])
        say(f"Email finder found {found} more emails from websites.")

    # 5. Filter: must have email
    final = [l for l in leads if l["contact_email"]]
    no_email = len(leads) - len(final)
    if no_email:
        say(f"Dropped {no_email} companies (no email found anywhere).")

    if not final:
        say("No leads with emails. Nothing to upload.")
        return

    # 6. Save CSV locally + upload to Drive
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = f"__{template}" if template else ""
    filename = f"prospect_{timestamp}{suffix}.csv"
    local_path = _save_csv(user_id, final, filename)
    say(f"Saved {len(final)} leads to `{filename}`.")

    try:
        drive_uploader.upload_csv(user_id, local_path, filename)
        say(f"Uploaded `{filename}` to Drive.")
    except Exception as e:
        log.error("Drive upload failed: %s", e)
        say(f"Drive upload failed: {e}\nCSV saved locally at `{local_path}`")

    # 7. Save new leads to Firestore (3 dedup keys + analytics fields)
    _save_prospects(final, user_id, queries)

    # 8. Summary
    say(f"*Prospect complete:*\n"
        f"  Scraped: {len(raw_leads)} businesses\n"
        f"  Already contacted: {skipped_sent}\n"
        f"  Previously prospected: {skipped_prospect}\n"
        f"  Emails from scraper: {has_email}\n"
        f"  Emails from website crawl: {sum(1 for l in missing if l['contact_email']) if missing else 0}\n"
        f"  No email (dropped): {no_email}\n"
        f"  *Ready in Drive: {len(final)}*\n\n"
        f"Check the CSV in Drive. When ready, use `auto` to start sending.")


# ── Scraper ────────────────────────────────────────────────────────────

def _run_scraper(user_id: str, queries: list[str], depth: int, debug: bool = False) -> str | None:
    """Run gosom scraper, return path to output CSV or None."""
    prospect_dir = _prospect_dir(user_id)
    os.makedirs(prospect_dir, exist_ok=True)

    queries_file = os.path.join(prospect_dir, "_queries.txt")
    with open(queries_file, "w", encoding="utf-8") as f:
        f.write("\n".join(queries) + "\n")

    output_file = os.path.join(prospect_dir, "_raw_results.csv")
    if os.path.exists(output_file):
        os.remove(output_file)

    cmd = [
        SCRAPER_PATH,
        "-input", queries_file,
        "-results", output_file,
        "-depth", str(depth),
        "-email",
        "-exit-on-inactivity", "3m",
    ]
    if debug:
        cmd.append("-debug")

    log.info("Running scraper: %s", " ".join(cmd))
    try:
        subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=SCRAPER_TIMEOUT)
    except subprocess.TimeoutExpired:
        log.error("Scraper timed out after %ds", SCRAPER_TIMEOUT)

    if os.path.exists(output_file) and os.path.getsize(output_file) > 0:
        return output_file
    return None


# ── CSV Parsing ────────────────────────────────────────────────────────

def _parse_gosom_csv(csv_path: str) -> list[dict]:
    leads = []
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            city, addr_state, country = "", "", ""
            try:
                addr = json.loads(row.get("complete_address", "{}") or "{}")
                city = addr.get("city", "")
                addr_state = addr.get("state", "")
                country = addr.get("country", "")
            except (json.JSONDecodeError, TypeError):
                pass

            # gmaps scraper sometimes returns a comma-list with URL-encoded prefixes
            # (e.g. "%20foo@bar.com, foo@bar.com" when site uses `mailto: foo@bar.com`).
            # Decode each candidate, strip whitespace, and pick the first non-junk one.
            email = ""
            for candidate in (row.get("emails") or "").split(","):
                cleaned = unquote(candidate).strip().lower()
                if cleaned and "@" in cleaned and not is_junk_email(cleaned):
                    email = cleaned
                    break

            lead = {
                "company_name": row.get("title", "").strip(),
                "contact_email": email,
                "website": row.get("website", "").strip(),
                "industry": row.get("category", "").strip(),
                "city": city,
                "state": addr_state,
                "country": country,
                "phone": row.get("phone", "").strip(),
                "review_rating": row.get("review_rating", "").strip(),
                "review_count": row.get("review_count", "").strip(),
            }

            if not lead["company_name"] or not lead["website"]:
                continue

            leads.append(lead)

    return leads




# ── Prospect Log ──────────────────────────────────────────────────────
# Multi-keyspace dedup: a lead is considered "previously prospected" if its
# domain, normalized phone, OR company_name+city+state matches anything in
# the global log. This catches companies that have no website (no domain)
# but appear in the scraper twice via different queries.

def _normalize_phone(phone: str) -> str:
    """Strip all non-digits; drop a leading US country code so +1-555-xxx and (555)xxx match."""
    digits = "".join(c for c in (phone or "") if c.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        return digits[1:]
    return digits


def _company_key(lead: dict) -> str:
    """Identifying key for leads with no website. Requires name AND city for safety
    (lots of "Joe's Pizza" exist; without a city the key is too ambiguous)."""
    name = " ".join((lead.get("company_name") or "").lower().split())
    city = (lead.get("city") or "").strip().lower()
    state = (lead.get("state") or "").strip().lower()
    if not name or not city:
        return ""
    return f"{name}|{city}|{state}"


def _load_prospect_log() -> dict:
    """Stream the global prospects collection into in-memory sets for O(1) dedup.

    Three keyspaces: domains / phones / companies. A lead is "previously
    prospected" if any of its identifying keys is in the corresponding set.
    Done once per prospect run; the few-hundred-doc scan is cheap.
    """
    sets: dict[str, set[str]] = {"domains": set(), "phones": set(), "companies": set()}
    for doc in prospects_col().stream():
        d = doc.to_dict()
        if d.get("domain"):
            sets["domains"].add(d["domain"])
        if d.get("phone"):
            sets["phones"].add(d["phone"])
        if d.get("company_key"):
            sets["companies"].add(d["company_key"])
    return sets


def _lead_is_prospected(lead: dict, log: dict) -> bool:
    """True if any of the lead's identifying keys (domain / phone / name+city) is in the log."""
    d = extract_domain(lead.get("website", ""))
    if d and d in log["domains"]:
        return True
    p = _normalize_phone(lead.get("phone", ""))
    if p and p in log["phones"]:
        return True
    c = _company_key(lead)
    if c and c in log["companies"]:
        return True
    return False


def _save_prospects(leads: list[dict], user_id: str, queries: list[str]) -> None:
    """Write each new lead to the global prospects collection with all three
    dedup keys plus analytics fields. Leads with no identifying key (no domain,
    phone, or company_key) are skipped — nothing to dedup against later."""
    col = prospects_col()
    db = col._client
    batch = db.batch()
    pending = 0
    written = 0
    now = datetime.now(tz=timezone.utc)
    query_str = ", ".join(queries) if queries else None

    for lead in leads:
        domain = extract_domain(lead.get("website", "")) or None
        phone = _normalize_phone(lead.get("phone", "")) or None
        company_key = _company_key(lead) or None
        if not (domain or phone or company_key):
            continue
        batch.set(col.document(), {
            "domain": domain,
            "phone": phone,
            "company_key": company_key,
            "prospected_at": now,
            "prospected_by_user_id": user_id,
            "query_used": query_str,
            "company_name": lead.get("company_name") or None,
            "industry": lead.get("industry") or None,
            "city": lead.get("city") or None,
            "state": lead.get("state") or None,
        })
        pending += 1
        if pending >= 400:
            batch.commit()
            written += pending
            batch = db.batch()
            pending = 0

    if pending:
        batch.commit()
        written += pending

    log.info("Wrote %d prospects to Firestore", written)


# ── Email Enrichment ───────────────────────────────────────────────────

def _enrich_emails(leads: list[dict]):
    with ThreadPoolExecutor(max_workers=EMAIL_FINDER_WORKERS) as pool:
        futures = {
            pool.submit(find_email, lead["website"]): lead
            for lead in leads
        }
        for future in as_completed(futures):
            lead = futures[future]
            try:
                email = future.result()
                if email:
                    lead["contact_email"] = email
            except Exception as e:
                log.debug("Email finder failed for %s: %s", lead["website"], e)


# ── Output ─────────────────────────────────────────────────────────

def _save_csv(user_id: str, leads: list[dict], filename: str) -> str:
    prospect_dir = _prospect_dir(user_id)
    os.makedirs(prospect_dir, exist_ok=True)
    path = os.path.join(prospect_dir, filename)
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        for lead in leads:
            writer.writerow({col: lead.get(col, "") for col in OUTPUT_COLUMNS})
    return path
