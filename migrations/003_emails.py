"""Migration 003: per-user sent_log.json + failed_log.json → Firestore
`users/{uid}/emails` collection.

Per firestore_schema.md the emails collection unifies the email lifecycle
under one status state machine. Legacy splits the data across two files:

    sent_log.json   = [{status="sent", thread_id, message_id, body, ...}]
    failed_log.json = [{failure_type="discarded", subject, contact_email, ...}]

Mapping:
  sent_log entry     → status="sent"
  failure_type=discarded → status="rejected_by_reviewer"
                           (GPT Reviewer 3-round failure)
  other failure_type     → status="failed"  (fallback)

Doc ID strategy (chosen for idempotency on re-run):
  sent:   gmail_message_id       (Gmail-issued, globally unique)
  failed: discarded_{contact_email}_{failed_at_compact}
          (legacy entries have no message_id — synthesized from
           contact + timestamp, stable on re-run)

Timestamp: legacy "sent_at"/"failed_at" are naive strings; users run
in Pacific Time, so we tag them as America/Los_Angeles and convert to
UTC for Firestore.

Usage:
    .venv/Scripts/python migrations/003_emails.py --dry-run
    .venv/Scripts/python migrations/003_emails.py --commit
    .venv/Scripts/python migrations/003_emails.py --dry-run --user demo_user
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from services.firestore_client import user_emails_col  # noqa: E402

DATA_DIR = REPO_ROOT / "data"
USERS_FILE = REPO_ROOT / "config" / "users.json"
PT = ZoneInfo("America/Los_Angeles")
LEGACY_TIME_FMT = "%Y-%m-%d %H:%M:%S"


def load_users() -> dict:
    """Return {uid: {name, dir, ...}} from config/users.json."""
    with USERS_FILE.open(encoding="utf-8") as f:
        return json.load(f)


def parse_pt_to_utc(ts_str: str) -> datetime:
    """Parse a naive 'YYYY-MM-DD HH:MM:SS' as PT, return tz-aware UTC datetime."""
    naive = datetime.strptime(ts_str, LEGACY_TIME_FMT)
    return naive.replace(tzinfo=PT).astimezone(timezone.utc)


def compact_ts(ts_str: str) -> str:
    """'2026-04-22 15:56:43' → '20260422_155643' for doc ID synthesis."""
    naive = datetime.strptime(ts_str, LEGACY_TIME_FMT)
    return naive.strftime("%Y%m%d_%H%M%S")


def map_sent(entry: dict, user_id: str) -> tuple[str, dict]:
    """Return (doc_id, doc) for a sent_log entry."""
    sent_at = parse_pt_to_utc(entry["sent_at"])
    doc_id = entry["message_id"]
    doc = {
        "campaign_id": entry["campaign_id"],
        "user_id": user_id,
        "template": None,
        "company_name": entry["company_name"],
        "contact_email": entry["contact_email"],
        "industry": entry.get("industry"),
        "source_drive_file_id": None,
        "source_csv_filename": None,
        "subject": entry["subject"],
        "body": entry["body"],
        "status": "sent",
        "created_at": sent_at,
        "approved_at": None,
        "approved_by": None,
        "sent_at": sent_at,
        "reviewer_rounds": None,
        "reviewer_scores": None,
        "reviewer_verdict": None,
        "rejected_reason": None,
        "error": None,
        "gmail_thread_id": entry.get("thread_id"),
        "gmail_message_id": entry["message_id"],
    }
    return doc_id, doc


def map_failed(entry: dict, user_id: str) -> tuple[str, dict]:
    """Return (doc_id, doc) for a failed_log entry."""
    failed_at = parse_pt_to_utc(entry["failed_at"])
    failure_type = entry.get("failure_type", "unknown")
    if failure_type == "discarded":
        status = "rejected_by_reviewer"
        rejected_reason = "GPT Reviewer 3-round failure (legacy 'discarded')"
        error = None
    else:
        status = "failed"
        rejected_reason = None
        error = failure_type
    doc_id = f"discarded_{entry['contact_email']}_{compact_ts(entry['failed_at'])}"
    doc = {
        "campaign_id": entry["campaign_id"],
        "user_id": user_id,
        "template": None,
        "company_name": entry["company_name"],
        "contact_email": entry["contact_email"],
        "industry": entry.get("industry"),
        "source_drive_file_id": None,
        "source_csv_filename": None,
        "subject": entry["subject"],
        "body": None,
        "status": status,
        "created_at": failed_at,
        "approved_at": None,
        "approved_by": None,
        "sent_at": None,
        "reviewer_rounds": None,
        "reviewer_scores": None,
        "reviewer_verdict": None,
        "rejected_reason": rejected_reason,
        "error": error,
        "gmail_thread_id": None,
        "gmail_message_id": None,
    }
    return doc_id, doc


def load_json_list(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"Expected list in {path}, got {type(data).__name__}")
    return data


def collect_for_user(uid: str, dir_name: str) -> list[tuple[str, dict]]:
    """Return list of (doc_id, doc) for one user, sent + failed combined."""
    user_dir = DATA_DIR / dir_name
    sent_entries = load_json_list(user_dir / "sent_log.json")
    failed_entries = load_json_list(user_dir / "failed_log.json")

    docs: list[tuple[str, dict]] = []
    for e in sent_entries:
        docs.append(map_sent(e, uid))
    for e in failed_entries:
        docs.append(map_failed(e, uid))
    return docs


def write_batch(uid: str, docs: list[tuple[str, dict]]) -> int:
    col = user_emails_col(uid)
    db = col._client
    batch = db.batch()
    pending = 0
    written = 0
    for doc_id, doc in docs:
        batch.set(col.document(doc_id), doc)
        pending += 1
        if pending >= 400:
            batch.commit()
            written += pending
            print(f"    committed {written}/{len(docs)}")
            batch = db.batch()
            pending = 0
    if pending:
        batch.commit()
        written += pending
    return written


def run(commit: bool, user_filter: str | None) -> None:
    users = load_users()
    targets = {
        uid: u
        for uid, u in users.items()
        if u.get("dir")
        and (DATA_DIR / u["dir"]).exists()
        and (user_filter is None or u.get("name", "").lower() == user_filter.lower() or u["dir"] == user_filter)
    }
    if not targets:
        print(f"No users to migrate (filter={user_filter!r}).")
        return

    grand_total = 0
    for uid, u in targets.items():
        print(f"--- {u['name']} ({uid}) — dir={u['dir']} ---")
        docs = collect_for_user(uid, u["dir"])
        sent_n = sum(1 for _, d in docs if d["status"] == "sent")
        rej_n = sum(1 for _, d in docs if d["status"] == "rejected_by_reviewer")
        fail_n = sum(1 for _, d in docs if d["status"] == "failed")
        print(f"  → sent: {sent_n}, rejected_by_reviewer: {rej_n}, failed: {fail_n}")

        if not docs:
            print("  (nothing to migrate)")
            continue

        if not commit:
            print("  Sample (first 2 docs):")
            for doc_id, doc in docs[:2]:
                print(f"    {doc_id}")
                print(f"      status={doc['status']}  campaign={doc['campaign_id']}  company={doc['company_name']!r}")
                ts = doc.get("sent_at") or doc.get("created_at")
                print(f"      timestamp (UTC) = {ts.isoformat() if ts else None}")
            grand_total += len(docs)
            continue

        n = write_batch(uid, docs)
        print(f"  Wrote {n} docs to users/{uid}/emails")
        grand_total += n

    print()
    if commit:
        print(f"Done. Total written: {grand_total}")
    else:
        print(f"Dry-run only. Total to write: {grand_total}")
        print("Re-run with --commit to write.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--commit", action="store_true")
    parser.add_argument("--user", help="Optional: filter by user name or dir (e.g. 'demo_user')")
    args = parser.parse_args()
    run(commit=args.commit, user_filter=args.user)


if __name__ == "__main__":
    main()
