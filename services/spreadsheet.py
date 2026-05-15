import logging

import pandas as pd

from services.firestore_client import user_emails_col

log = logging.getLogger(__name__)

_COLUMN_MAP = {
    "company name": "company_name",
    "company": "company_name",
    "contact email": "contact_email",
    "email": "contact_email",
    "email address": "contact_email",
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
    "first name": "first_name",
    "last name": "last_name",
    "job title": "job_title",
    "phone": "phone",
    "direct phone number": "phone",
    "company street address": "address",
    "review_rating": "review_rating",
    "review rating": "review_rating",
    "rating": "review_rating",
    "review_count": "review_count",
    "review count": "review_count",
    "reviews": "review_count",
    "state": "state",
}

REQUIRED_FIELDS = {"company_name", "contact_email"}


def _load_sent_emails(user_id: str) -> set[str]:
    """Lowercased contact_emails for every successfully-sent email by this user.
    Used to dedup against future prospect runs / spreadsheet imports so we
    don't email the same address twice."""
    return {
        (doc.to_dict().get("contact_email") or "").lower()
        for doc in user_emails_col(user_id).where("status", "==", "sent").stream()
    } - {""}


def parse_dataframe(user_id: str, df: pd.DataFrame) -> list[dict]:
    rename = {}
    for col in df.columns:
        key = col.strip().lower()
        if key in _COLUMN_MAP:
            rename[col] = _COLUMN_MAP[key]
    df = df.rename(columns=rename)
    # 去掉重复列名（如 ZoomInfo 的 Employees + Employee Range 都映射到 employees）
    df = df.loc[:, ~df.columns.duplicated()]

    missing = REQUIRED_FIELDS - set(df.columns)
    if missing:
        log.error("Spreadsheet missing required columns: %s", missing)
        return []

    sent_emails = _load_sent_emails(user_id)
    companies = []

    for _, row in df.iterrows():
        record = {}
        for col in df.columns:
            val = row[col]
            record[col] = "" if pd.isna(val) else str(val).strip()

        if not record.get("contact_email"):
            continue

        if record["contact_email"].lower() in sent_emails:
            log.info("Skipping already-sent: %s", record["contact_email"])
            continue

        if not record.get("contact_name"):
            first = record.get("first_name", "").strip()
            last = record.get("last_name", "").strip()
            if first or last:
                record["contact_name"] = f"{first} {last}".strip()
            else:
                record["contact_name"] = f"{record.get('company_name', 'Unknown')}'s friends"

        companies.append(record)

    log.info("Parsed %d companies (%d after dedup)", len(df), len(companies))
    return companies
