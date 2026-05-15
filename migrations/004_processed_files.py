"""Migration 004: per-user processed_files.json → Firestore
`users/{uid}/processed_files` collection.

Schema: doc_id = Drive file_id, body = {file_name, processed_at}.

Legacy JSON only stores a list of file_ids — no name, no timestamp. So:
  - file_name backfill: "<backfilled>" (real names will be written next
    time mark_processed runs)
  - processed_at backfill: SERVER_TIMESTAMP (when the migration runs)

Dedup logic only reads doc id, so the placeholder name doesn't affect
correctness. It's only there for human debugging.

Usage:
    .venv/Scripts/python migrations/004_processed_files.py --dry-run
    .venv/Scripts/python migrations/004_processed_files.py --commit
    .venv/Scripts/python migrations/004_processed_files.py --commit --user luna
"""

import argparse
import json
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from google.cloud import firestore  # noqa: E402
from services.firestore_client import user_processed_files_col  # noqa: E402

DATA_DIR = REPO_ROOT / "data"
USERS_FILE = REPO_ROOT / "config" / "users.json"

BACKFILL_NAME = "<backfilled>"


def load_users() -> dict:
    with USERS_FILE.open(encoding="utf-8") as f:
        return json.load(f)


def migrate_user(user_id: str, user_dir: str, commit: bool) -> tuple[int, int]:
    """Return (planned, written). On dry-run, written = 0."""
    legacy = DATA_DIR / user_dir / "processed_files.json"
    if not legacy.exists():
        print(f"  [{user_dir}] no processed_files.json — skip")
        return 0, 0

    with legacy.open(encoding="utf-8") as f:
        ids = json.load(f)

    if not ids:
        print(f"  [{user_dir}] empty list — skip")
        return 0, 0

    print(f"  [{user_dir}] {len(ids)} file_id(s) to migrate")
    if not commit:
        for fid in ids:
            print(f"    DRY: {fid}")
        return len(ids), 0

    col = user_processed_files_col(user_id)
    for fid in ids:
        col.document(fid).set({
            "file_name": BACKFILL_NAME,
            "processed_at": firestore.SERVER_TIMESTAMP,
        })
        print(f"    WROTE: {fid}")
    return len(ids), len(ids)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="Show plan, don't write Firestore")
    ap.add_argument("--commit", action="store_true", help="Actually write")
    ap.add_argument("--user", help="Migrate just this user (by dir name)")
    args = ap.parse_args()

    if args.dry_run == args.commit:
        ap.error("Pick exactly one of --dry-run or --commit")

    users = load_users()
    total_planned = total_written = 0
    print(f"Mode: {'COMMIT' if args.commit else 'DRY-RUN'}")
    for uid, info in users.items():
        user_dir = info.get("dir")
        if not user_dir:
            continue
        if args.user and user_dir != args.user:
            continue
        p, w = migrate_user(uid, user_dir, args.commit)
        total_planned += p
        total_written += w

    print(f"\nTotal: planned={total_planned}, written={total_written}")


if __name__ == "__main__":
    main()
