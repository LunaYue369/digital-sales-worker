"""Prospect pipeline — per-user. Find leads via Google Maps scraper, enrich emails, upload to Drive.

Only generates CSV and uploads to Drive. Does NOT send emails.
"""

import csv
import json
import logging
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from services import email_finder, drive_uploader
from services.spreadsheet import _load_sent_emails
from core.user_config import user_data_dir

log = logging.getLogger(__name__)

SCRAPER_PATH = os.getenv("GMAPS_SCRAPER_PATH", r"C:\Users\Luna\repos\google-maps-scraper\gmaps-scraper.exe")
SCRAPER_DEPTH = int(os.getenv("PROSPECT_SCRAPER_DEPTH", "1"))
SCRAPER_TIMEOUT = int(os.getenv("PROSPECT_SCRAPER_TIMEOUT", "1800"))
EMAIL_FINDER_WORKERS = int(os.getenv("PROSPECT_EMAIL_WORKERS", "5"))

OUTPUT_COLUMNS = [
    "company_name", "contact_email", "website", "industry",
    "city", "state", "country", "phone",
    "review_rating", "review_count",
]


def _prospect_dir(user_id: str) -> str:
    return os.path.join(user_data_dir(user_id), "prospect_results")


def _prospect_log_path(user_id: str) -> str:
    return os.path.join(user_data_dir(user_id), "prospect_log.json")


def run_prospect(user_id: str, queries: list[str], say, depth: int | None = None):
    """One-shot: run scraper → find emails → upload CSV to Drive."""
    say(f"Prospect started with {len(queries)} search queries:\n"
        + "\n".join(f"  • {q}" for q in queries))

    # 1. Run gosom scraper
    effective_depth = depth or SCRAPER_DEPTH
    say(f"Running Google Maps scraper (depth={effective_depth}, this may take a few minutes)...")
    raw_csv = _run_scraper(user_id, queries, effective_depth)
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

    prospected_domains = _load_prospect_log(user_id)

    leads = []
    skipped_sent = 0
    skipped_prospect = 0
    for l in raw_leads:
        domain = _extract_domain(l["website"])
        if domain in sent_domains or l["contact_email"].lower() in sent_emails:
            skipped_sent += 1
        elif domain in prospected_domains:
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
    filename = f"prospect_{timestamp}.csv"
    local_path = _save_csv(user_id, final, filename)
    say(f"Saved {len(final)} leads to `{filename}`.")

    try:
        drive_uploader.upload_csv(user_id, local_path, filename)
        say(f"Uploaded `{filename}` to Drive.")
    except Exception as e:
        log.error("Drive upload failed: %s", e)
        say(f"Drive upload failed: {e}\nCSV saved locally at `{local_path}`")

    # 7. Save new domains to prospect_log
    new_domains = {_extract_domain(l["website"]) for l in final}
    _save_prospect_log(user_id, new_domains)

    # 8. Summary
    say(f"*Prospect complete:*\n"
        f"  Scraped: {len(raw_leads)} businesses\n"
        f"  Already contacted: {skipped_sent}\n"
        f"  Previously prospected: {skipped_prospect}\n"
        f"  Emails from scraper: {has_email}\n"
        f"  Emails from website crawl: {sum(1 for l in missing if l['contact_email']) if missing else 0}\n"
        f"  No email (dropped): {no_email}\n"
        f"  *Ready in Drive: {len(final)}*\n\n"
        f"Check the CSV in Drive. When ready, use `/ auto` to start sending.")


# ── Scraper ────────────────────────────────────────────────────────────

def _run_scraper(user_id: str, queries: list[str], depth: int) -> str | None:
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

            raw_email = (row.get("emails") or "").strip()
            email = raw_email.split(",")[0].strip() if raw_email else ""

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


def _extract_domain(website: str) -> str:
    w = website.strip().lower()
    for prefix in ("https://", "http://", "www."):
        w = w.removeprefix(prefix)
    return w.rstrip("/").split("/")[0]


# ── Prospect Log ──────────────────────────────────────────────────────

def _load_prospect_log(user_id: str) -> set[str]:
    path = _prospect_log_path(user_id)
    if not os.path.exists(path):
        return set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return set(data.get("domains", {}).keys())
    except (json.JSONDecodeError, OSError):
        return set()


def _save_prospect_log(user_id: str, new_domains: set[str]):
    path = _prospect_log_path(user_id)
    existing = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                existing = json.load(f).get("domains", {})
        except (json.JSONDecodeError, OSError):
            pass

    today = datetime.now().strftime("%Y-%m-%d")
    for domain in new_domains:
        if domain and domain not in existing:
            existing[domain] = today

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"domains": existing}, f, indent=2, ensure_ascii=False)


# ── Email Enrichment ───────────────────────────────────────────────────

def _enrich_emails(leads: list[dict]):
    with ThreadPoolExecutor(max_workers=EMAIL_FINDER_WORKERS) as pool:
        futures = {
            pool.submit(email_finder.find_email, lead["website"]): lead
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
