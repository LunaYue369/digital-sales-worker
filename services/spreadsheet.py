"""Parse spreadsheet DataFrame into standardized company records, with dedup."""

import json
import logging
import os

import pandas as pd

log = logging.getLogger(__name__)

SENT_LOG_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "sent_log.json")

# Column name mapping — normalize various header names to our standard fields
_COLUMN_MAP = {
    "company name": "company_name",
    "company": "company_name",
    "contact email": "contact_email",
    "email": "contact_email",
    "website": "website",
    "primary industry": "industry",
    "industry": "industry",
    "core business": "core_business",
    "business": "core_business",
    "company country": "country",
    "country": "country",
    "company city": "city",
    "city": "city",
    "revenue range (in usd)": "revenue",
    "revenue": "revenue",
    "employee range": "employees",
    "employees": "employees",
    "contact name": "contact_name",
    "contact": "contact_name",
}

REQUIRED_FIELDS = {"company_name", "contact_email"}


def _load_sent_emails() -> set[str]:
    if os.path.exists(SENT_LOG_PATH):
        with open(SENT_LOG_PATH, "r", encoding="utf-8") as f:
            records = json.load(f)
        return {r["contact_email"].lower() for r in records}
    return set()


def parse_dataframe(df: pd.DataFrame) -> list[dict]:
    """Normalize columns and return list of company dicts, deduped against sent_log."""
    # Normalize column names
    rename = {}
    for col in df.columns:
        key = col.strip().lower()
        if key in _COLUMN_MAP:
            rename[col] = _COLUMN_MAP[key]
    df = df.rename(columns=rename)

    # Check required fields
    missing = REQUIRED_FIELDS - set(df.columns)
    if missing:
        log.error("Spreadsheet missing required columns: %s", missing)
        return []

    sent_emails = _load_sent_emails()
    companies = []

    for _, row in df.iterrows():
        record = {}
        for col in df.columns:
            val = row[col]
            record[col] = "" if pd.isna(val) else str(val).strip()

        if not record.get("contact_email"):
            continue

        # Dedup: skip already-sent emails
        if record["contact_email"].lower() in sent_emails:
            log.info("Skipping already-sent: %s", record["contact_email"])
            continue

        # Default contact_name from email if missing
        if not record.get("contact_name"):
            record["contact_name"] = record["contact_email"].split("@")[0].replace(".", " ").title()

        companies.append(record)

    log.info("Parsed %d companies (%d after dedup)", len(df), len(companies))
    return companies
