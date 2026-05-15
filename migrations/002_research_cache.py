"""Migration 002: data/research_cache.json → Firestore `research_cache` collection.

Source shape (legacy):
    {"<domain>": {"brief": {...}, "company_info": {...}, "cached_at": <unix_ts>}}

Target shape (per firestore_schema.md):
    research_cache/{domain} = {
        domain, brief, company_info, cached_at, expires_at
    }
    expires_at = cached_at + 30 days  (drives Firestore TTL policy)

Doc ID = domain → re-running set() naturally overwrites, idempotent.

Filters (two gates, mirroring the live code's behavior in agents/researcher.py):
  1. _is_brief_valid(brief) — drop GPT garbage / placeholder briefs
  2. cached_at < CACHE_TTL_DAYS old — drop already-expired entries
     (the live code's _check_cache rejects them anyway)

Usage:
    .venv/Scripts/python migrations/002_research_cache.py --dry-run
    .venv/Scripts/python migrations/002_research_cache.py --commit
"""

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from agents.researcher import _is_brief_valid, CACHE_TTL_DAYS  # noqa: E402
from services.email_finder import extract_domain  # noqa: E402
from services.firestore_client import research_cache_col  # noqa: E402

SOURCE_FILE = REPO_ROOT / "data" / "research_cache.json"
TTL = timedelta(days=CACHE_TTL_DAYS)


def load_legacy() -> dict:
    with SOURCE_FILE.open(encoding="utf-8") as f:
        return json.load(f)


def classify(entry: dict, now_ts: float) -> tuple[str, str]:
    """Return (verdict, reason). verdict ∈ {'keep', 'skip'}."""
    if not isinstance(entry, dict):
        return "skip", "entry not a dict"
    brief = entry.get("brief")
    if not _is_brief_valid(brief):
        return "skip", "invalid brief (old shape or GPT garbage)"
    cached_at = entry.get("cached_at")
    if not isinstance(cached_at, (int, float)):
        return "skip", "missing/bad cached_at"
    age_days = (now_ts - cached_at) / 86400
    if age_days > CACHE_TTL_DAYS:
        return "skip", f"expired (age={age_days:.1f}d)"
    return "keep", ""


def build_doc(domain: str, entry: dict) -> dict:
    cached_at = datetime.fromtimestamp(entry["cached_at"], tz=timezone.utc)
    return {
        "domain": domain,
        "brief": entry["brief"],
        "company_info": entry.get("company_info", {}),
        "cached_at": cached_at,
        "expires_at": cached_at + TTL,
    }


def run(commit: bool) -> None:
    legacy = load_legacy()
    now_ts = time.time()
    print(f"→ Legacy entries: {len(legacy)}")

    # First pass: filter + normalize key. Some legacy keys are dirty URLs
    # (e.g. `foo.com/?utm_source=gbp`) — normalize with extract_domain.
    # Multiple dirty keys may collapse to the same clean domain — keep the
    # most recently cached entry on collision.
    keep: dict[str, dict] = {}
    skip_reasons: dict[str, int] = {}
    collisions = 0
    normalizations = 0
    for raw_key, entry in legacy.items():
        verdict, reason = classify(entry, now_ts)
        if verdict != "keep":
            skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
            continue
        clean = extract_domain(raw_key)
        if not clean:
            skip_reasons["empty after extract_domain"] = (
                skip_reasons.get("empty after extract_domain", 0) + 1
            )
            continue
        if clean != raw_key:
            normalizations += 1
        if clean in keep:
            collisions += 1
            # Keep the newer cached_at
            if entry["cached_at"] > keep[clean]["cached_at"]:
                keep[clean] = entry
        else:
            keep[clean] = entry

    print(f"→ Passed filters: {len(keep)}")
    print(f"→ Normalized keys: {normalizations}")
    print(f"→ Collisions (multiple dirty keys → same clean domain): {collisions}")
    print(f"→ Skipped: {sum(skip_reasons.values())}")
    for reason, count in sorted(skip_reasons.items(), key=lambda x: -x[1]):
        print(f"    {count:4d}× {reason}")

    if not keep:
        print("Nothing to migrate. Done.")
        return

    if not commit:
        print()
        print("Sample of what would be written (first 3):")
        for domain in list(keep)[:3]:
            d = build_doc(domain, keep[domain])
            print(f"  {domain}")
            print(f"    cached_at:  {d['cached_at'].isoformat()}")
            print(f"    expires_at: {d['expires_at'].isoformat()}")
            print(f"    brief keys: {list(d['brief'].keys())}")
        print()
        print("Dry-run only. Re-run with --commit to write.")
        return

    col = research_cache_col()
    batch = col._client.batch()
    pending = 0
    written = 0
    BATCH_SIZE = 400

    for domain, entry in keep.items():
        batch.set(col.document(domain), build_doc(domain, entry))
        pending += 1
        if pending >= BATCH_SIZE:
            batch.commit()
            written += pending
            print(f"  committed {written}/{len(keep)}")
            batch = col._client.batch()
            pending = 0

    if pending:
        batch.commit()
        written += pending

    print(f"✅ Wrote {written} docs to research_cache collection.")
    print()
    print("Next step (one-time, run in another terminal):")
    print("  gcloud firestore fields ttls update expires_at \\")
    print("    --collection-group=research_cache --enable-ttl")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--commit", action="store_true")
    args = parser.parse_args()
    run(commit=args.commit)


if __name__ == "__main__":
    main()
