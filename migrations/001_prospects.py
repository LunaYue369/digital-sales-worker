"""Migration 001: data/prospect_log.json → Firestore `prospects` collection.

Source shape (legacy):
    {"domains": {"<domain>": "<YYYY-MM-DD>", ...}}    # 177 entries

Target shape (new schema, see firestore_schema.md):
    prospects/{auto_id} = {
        domain, phone, company_key,
        prospected_at, prospected_by_user_id, query_used,
        company_name, industry, city, state
    }

Legacy file only carries `domain` + a date — everything else is null.
Re-run-safe: skips domains that already exist in Firestore.

Usage:
    .venv/Scripts/python migrations/001_prospects.py --dry-run   # plan
    .venv/Scripts/python migrations/001_prospects.py --commit    # write
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Force UTF-8 stdout so Windows GBK console doesn't choke on log glyphs.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from services.firestore_client import prospects_col  # noqa: E402

SOURCE_FILE = REPO_ROOT / "data" / "prospect_log.json"


def load_legacy() -> dict[str, str]:
    """Return {domain: iso_date} from the legacy JSON."""
    with SOURCE_FILE.open(encoding="utf-8") as f:
        data = json.load(f)
    domains = data.get("domains", {})
    if not isinstance(domains, dict):
        raise ValueError(f"Expected dict under 'domains', got {type(domains).__name__}")
    return domains


def existing_domains() -> set[str]:
    """Pull every `domain` value already in the prospects collection."""
    return {
        doc.get("domain")
        for doc in prospects_col().stream()
        if doc.get("domain")
    }


def to_timestamp(iso_date: str) -> datetime:
    """Parse 'YYYY-MM-DD' → tz-aware UTC datetime (Firestore serializes to Timestamp)."""
    return datetime.strptime(iso_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def build_doc(domain: str, iso_date: str) -> dict:
    return {
        "domain": domain,
        "phone": None,
        "company_key": None,
        "prospected_at": to_timestamp(iso_date),
        "prospected_by_user_id": None,
        "query_used": None,
        "company_name": None,
        "industry": None,
        "city": None,
        "state": None,
    }


def run(commit: bool) -> None:
    legacy = load_legacy()
    print(f"→ Legacy entries: {len(legacy)}")

    already = existing_domains()
    print(f"→ Already in Firestore: {len(already)}")

    todo = {d: date for d, date in legacy.items() if d not in already}
    print(f"→ To write: {len(todo)}")

    if not todo:
        print("Nothing to migrate. Done.")
        return

    if not commit:
        sample = list(todo.items())[:5]
        print()
        print("Sample of what would be written (first 5):")
        for d, date in sample:
            print(f"  {d}  →  prospected_at={date}")
        print()
        print("Dry-run only. Re-run with --commit to write.")
        return

    col = prospects_col()
    batch = col._client.batch()
    pending = 0
    written = 0
    BATCH_SIZE = 400  # Firestore batch limit is 500, leave headroom

    for domain, iso_date in todo.items():
        doc_ref = col.document()  # auto_id
        batch.set(doc_ref, build_doc(domain, iso_date))
        pending += 1
        if pending >= BATCH_SIZE:
            batch.commit()
            written += pending
            print(f"  committed {written}/{len(todo)}")
            batch = col._client.batch()
            pending = 0

    if pending:
        batch.commit()
        written += pending

    print(f"✅ Wrote {written} docs to prospects collection.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Plan only, no writes")
    mode.add_argument("--commit", action="store_true", help="Actually write to Firestore")
    args = parser.parse_args()
    run(commit=args.commit)


if __name__ == "__main__":
    main()
