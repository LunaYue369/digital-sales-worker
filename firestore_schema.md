# Firestore Schema — digital-sales-worker

GCP project: `your-gcp-project` · Database: `(default)` · Mode: Native · Region: `nam5`

---

## Auth

| Environment | Method |
|---|---|
| Local development | Application Default Credentials via `gcloud auth application-default login` (your dev account) |
| Production deploy | TBD — either lift `iam.disableServiceAccountKeyCreation` org policy for this project, or set up Workload Identity Federation |

Code uses `google.cloud.firestore.Client(project="your-gcp-project")`. Auth is auto-discovered (env var `GOOGLE_APPLICATION_CREDENTIALS` if a SA key is present, else ADC).

---

## Collections Overview

| Path | Scope | Replaces | Doc ID strategy |
|---|---|---|---|
| `prospects` | Global, shared across all sales | `data/prospect_log.json` | Prefixed key (see below) |
| `research_cache` | Global | `data/research_cache.json` | Domain |
| `users/{uid}/emails` | Per-sale | `sent_log.json` + `failed_log.json` + new draft state | Auto-id |
| `users/{uid}/usage` | Per-sale | `usage_log.json` | Auto-id |
| `users/{uid}/replies` | Per-sale | future `reply_log.json` | Gmail `thread_id` |
| `users/{uid}/processed_files` | Per-sale | `processed_files.json` | Drive `file_id` |

---

## `prospects` (global, single doc per lead)

A lead is "previously prospected" if **any** of its identifying fields (`domain`, `phone`, `company_key`) matches an existing doc. One doc per lead, auto-generated doc ID, dedup by `where` queries on indexed fields.

```
prospects/{auto_id}
```

### Document shape

```json
{
  "domain": "acmeco.example.com",       // nullable
  "phone": "+15551234567",                  // nullable, E.164 normalized
  "company_key": "acme co|san francisco|ca",  // nullable, lowercased
  "prospected_at": <Timestamp>,
  "prospected_by_user_id": "U_DEMO_USER",
  "query_used": "HVAC contractors in San Francisco",
  "company_name": "Acme Co",
  "industry": "HVAC contractor",
  "city": "San Francisco",
  "state": "CA"
}
```

At least one of `domain` / `phone` / `company_key` must be non-null (otherwise the lead has no identity to dedup on — skip it upstream).

### Lookup pattern

```python
def already_prospected(domain, phone, company_key) -> bool:
    col = prospects_col()
    for field, value in (("domain", domain), ("phone", phone), ("company_key", company_key)):
        if not value:
            continue
        hits = col.where(field, "==", value).limit(1).get()
        if hits:
            return True
    return False
```

Up to 3 indexed where-queries per lead. Firestore auto-creates single-field indexes on each — no manual index work needed. With 100 leads = ~300 reads, free tier 50K/day, well under.

**Concurrency note:** current `prospect` pipeline is single-process / sequential per user, so the "two callers both see no-hit and both insert" race doesn't exist. If it ever goes concurrent, wrap the check+write in a Firestore transaction.

### Why single doc, not prefix-keyed multi-doc

An earlier draft used `prospects/d:<domain>` / `prospects/p:<phone>` / `prospects/c:<company_key>` — three docs per lead, doc-ID lookup for O(1) reads. Rejected because:

- A lead is one logical entity; three docs misrepresents that
- Updating any lead field (rename, new phone) means syncing three docs
- Lookup-cost difference (3× doc.get vs 3× where-limit-1) is invisible at our scale
- Atomic-dedup-by-doc-ID benefit only matters under write concurrency we don't have

### Migration source

Existing `data/prospect_log.json` (177 domains) → 177 docs with `domain` set, `phone`/`company_key` null. Future prospect runs populate all three.

---

## `research_cache` (global, TTL 30 days)

```
research_cache/{domain}
```

### Document shape

```json
{
  "domain": "acmeco.example.com",
  "brief": {
    "reasoning": "...",
    "pain_point": "...",
    "talking_points": ["..."],
    "personalization_hooks": ["..."]
  },
  "company_info": {
    "company_name": "Acme Co",
    "website": "acmeco.example.com",
    "industry": "..."
  },
  "cached_at": <Timestamp>,
  "expires_at": <Timestamp>  // cached_at + 30 days, used by Firestore TTL policy
}
```

### TTL policy

Configure a Firestore TTL policy on field `expires_at`. Firestore auto-deletes expired docs within ~24 hours. No manual cleanup needed.

```bash
gcloud firestore fields ttls update expires_at \
  --collection-group=research_cache \
  --enable-ttl
```

### Validation gate (carries over from `agents/researcher.py:_is_brief_valid`)

Briefs that fail validation (empty pain_point, garbage reasoning, JSON parse failure) are NOT written to cache. Re-research on next encounter.

### Migration source

Existing `data/research_cache.json` (~6000+ companies, 420KB) → 6000+ docs.

### Migration choice: migrate, with a validation filter

6000 writes = 30% of a day's free tier — fine in a single window. Migrating beats letting the cache warm up from empty because:

- **Saves ~$4.20 in GPT cost** (6000 × ~$0.0007 per Researcher call)
- **Day-1 prospect runs hit the cache instead of re-researching** — meaningful speedup the first few weeks
- **Migration script is ~30 lines**, similar shape to the prospects migration

**Validation gate during migration**: each entry's `brief` is run through `_is_brief_valid()` (the same filter we already gate live writes with). Briefs with empty pain_point / "Could not parse" / sub-30-char reasoning are dropped. Expected drop rate ~5-10% (data written before that filter existed). Final migrated count: ~5500-5700 docs.

---

## `users/{uid}/emails` — unified email lifecycle

One document per email (or email attempt). The `status` field tracks the entire lifecycle.

### Status state machine (8 states)

```
drafted ─→ reviewing ─→ approved ─→ sending ─→ sent
            │              ↑                  └─→ failed
            ├─→ rejected_by_reviewer  (GPT Reviewer 3-round failure)
            └─→ rejected_by_human     (Slack reject button)

            (non-sampled) ─┘ (GPT Reviewer approved directly)

Any non-terminal → canceled (via stop auto)
```

Two distinct rejection statuses — the rejection source is in the status itself, not in a side field. Query "all rejections" with `status in ["rejected_by_reviewer", "rejected_by_human"]`.

**Terminal states**: `sent`, `failed`, `rejected_by_reviewer`, `rejected_by_human`, `canceled`.

### Document shape

```json
{
  "campaign_id": "campaign_eb49b4b6",
  "user_id": "U_DEMO_USER",
  "template": "comprehensive",
  "company_name": "Demo HVAC Co",
  "contact_email": "contact@demohvac.example.com",
  "industry": "HVAC contractor",

  // Source traceability — every email points back to the Drive CSV it came from
  "source_drive_file_id": "1abc...",
  "source_csv_filename": "prospect_20260101_120000__comprehensive.csv",

  "subject": "service calls at Demo HVAC Co",
  "body": "Hello,\n\nChecked out...",

  "status": "sent",
  "created_at": <Timestamp>,
  "approved_at": <Timestamp>,
  "approved_by": "human" | "gpt_reviewer" | null,
  "sent_at": <Timestamp>,

  "reviewer_rounds": 1,
  "reviewer_scores": {"professionalism": 8, "tone": 9, ...},
  "reviewer_verdict": "Clear value prop...",

  "rejected_reason": null,        // populated on status=rejected_by_*
  "error": null,                  // populated on status=failed
  "gmail_thread_id": "demo_thread_id_abc123",
  "gmail_message_id": "demo_thread_id_abc123"
}
```

### Common queries

```python
# Inspect: "draft <company_name>" Slack command
emails.where("company_name", "==", name)\
      .where("status", "in", ["drafted", "reviewing", "approved"])\
      .order_by("created_at", direction="DESCENDING").limit(1)

# Sender thread: pick up approved drafts
emails.where("status", "==", "approved").stream()

# Reporter: campaign stats
emails.where("campaign_id", "==", cid).stream()  # then group by status in code

# Stop auto: cancel all in-flight
batch = db.batch()
for doc in emails.where("campaign_id", "==", cid)\
                 .where("status", "in", ["drafted", "reviewing", "approved", "sending"])\
                 .stream():
    batch.update(doc.reference, {"status": "canceled"})
batch.commit()

# All rejections (either source) for a campaign
emails.where("campaign_id", "==", cid)\
      .where("status", "in", ["rejected_by_human", "rejected_by_reviewer"])\
      .stream()
```

### Indexes needed

- Single-field indexes auto-created (status, campaign_id, company_name, contact_email)
- Composite index: `(company_name ASC, status IN, created_at DESC)` for inspect command
- Composite index: `(campaign_id ASC, status IN)` for stop-auto + reporter

Firestore Console will prompt to create these on first failed query.

### Migration source

Existing `sent_log.json` per user → docs with `status="sent"`. Existing `failed_log.json` → docs with `status="failed"`. No drafts/reviewing data to migrate (that's a new state).

---

## `users/{uid}/usage` — token cost tracking

```
users/U_DEMO_USER/usage/{auto_id}
```

### Document shape

```json
{
  "campaign_id": "campaign_eb49b4b6",
  "step": "copywriter",
  "prompt_tokens": 1282,
  "completion_tokens": 126,
  "estimated_cost": 0.000714,
  "timestamp": <Timestamp>
}
```

Identical to current `usage_log.json` records. Simplest migration.

---

## `users/{uid}/replies`

```
users/U_DEMO_USER/replies/{thread_id}
```

`thread_id` as doc ID lets us idempotently update on re-poll without dupes.

### Document shape

```json
{
  "thread_id": "demo_thread_id_abc123",
  "from_name": "Demo Contact",
  "from_email": "contact@demohvac.example.com",
  "subject": "Re: service calls at...",
  "body": "Hi <Sales>,\n\nThanks for reaching out...",
  "received_at": <Timestamp>,
  "analysis": {
    "sentiment": "interested",
    "intent": "asking_question",
    "follow_up_advice": "...",
    ...
  },
  "analyzed_at": <Timestamp>,
  "linked_email_id": "<emails doc id>"  // back-pointer to the email this is replying to
}
```

---

## `users/{uid}/processed_files` — per-user Drive dedup (added 2026-05-14)

Tracks which Drive CSVs `auto` has already processed for each sales user. Replaces `data/{user}/processed_files.json`.

```
users/{uid}/processed_files/{file_id}
```

`{file_id}` is the Drive file ID (the `id` field returned by `drive.files().list`). Stable across renames; unique across all Drive.

### Document shape

```json
{
  "file_name": "prospect_20260101_120000__comprehensive.csv",
  "processed_at": "2026-01-01T12:00:00Z"
}
```

| Field | Why |
|---|---|
| `file_name` | Human-readable debug only — dedup never reads this. Drive file names can be renamed; doc id (`file_id`) is the stable key. |
| `processed_at` | Audit ("when did this CSV get sent?"). |

Drive metadata (mimeType, size, owner, etc.) is **not** mirrored here — Drive is the source of truth, look it up there.

### Why a dedicated collection (not `emails.where(source_drive_file_id == X)`)

Earlier plan was to derive "processed" from the emails collection. Rejected for two reasons:

1. **0-email CSV**: if a CSV produces no emails (all rows filtered by sent-email dedup or `contact_email` missing), no email doc gets written → reverse lookup says "never processed" → next `auto` re-processes the CSV → Researcher + Copywriter + Reviewer all run again → GPT cost wasted. A dedicated `processed_files` collection records the file as processed regardless of email outcome.
2. **Read pattern**: `auto` needs the full set of processed file_ids up front to filter `drive.files().list` results. Streaming a small per-user collection is cheaper and clearer than running N `where()` queries on emails.

---

## Campaign-level stats — derive from emails

No dedicated campaign collection. Reporter / `report` Slack command computes campaign stats by querying emails:

```python
# All emails for this campaign
docs = emails.where("campaign_id", "==", cid).stream()
# Group by status in code: sent / failed / rejected / canceled
```

For typical scale (~100 emails per campaign × ~10 campaigns/month), scanning is fast and cheap. Migrate to a cached aggregate doc only if reports get slow.

---

## Conventions

- All timestamps use Firestore `Timestamp` type (not ISO strings) — comparable, sortable, timezone-safe
- Field naming: `snake_case` (matches existing JSON shape)
- Don't store secrets / OAuth tokens in Firestore (those stay in `gmail_token.json` per user)
- Don't store full prospect CSV rows here — those stay in Drive (audit trail). `users/{uid}/processed_drive_files` only references the file IDs

---

## Migration Strategy

**Goal**: migrate every piece of local file-based runtime data that has a target in this schema. Nothing is left behind.

### What gets migrated

| Source file | Target collection | Approx volume |
|---|---|---|
| `data/prospect_log.json` (global, 177 entries) | `prospects` | 177 docs |
| `data/research_cache.json` (global, ~6000 entries) | `research_cache` | ~5500-5700 docs (after validation filter) |
| `data/{user}/sent_log.json` | `users/{uid}/emails` (status=sent) | ~150+ per user |
| `data/{user}/failed_log.json` | `users/{uid}/emails` (status=failed) | <20 per user |
| `data/{user}/usage_log.json` | `users/{uid}/usage` | ~6700+ per user |

### What does NOT get migrated (intentional)

| Source | Why not |
|---|---|
| `data/{user}/prospect_results/*.csv` | Drive is the source of truth for the lead CSV. Don't duplicate into Firestore. (Each migrated email's `source_drive_file_id` field will reference back.) |
| `data/{user}/processed_files.json` | Migrated 2026-05-14 to `users/{uid}/processed_files` (see migration 004). Legacy JSON kept as cold backup. |
| `data/{user}/reply_log.json` | File doesn't exist yet — `track` hasn't produced replies. Will be written natively to Firestore once `track` runs. |
| Per-user `data/{user}/prospect_log.json` (already deleted) | Already merged into global `data/prospect_log.json` in earlier work. |

### Sequence (smallest → largest, dual-write for live data)

1. **`prospects`** — small (~177 docs), validates the end-to-end write/read pattern + lookup helpers. The "smoke test" migration.
2. **`research_cache`** — same pattern as prospects but bigger (~5500-5700 docs after validation filter). Run during a quiet window.
3. **`users/{uid}/usage`** — informational only, low risk. Modest volume per user (~835 docs per active sales rep) but write-only path.
4. **`users/{uid}/emails`** — biggest impact (sender / reviewer / inspect command all touch this). Dual-write phase recommended (file + Firestore both written for ~1 week, compare; then drop file).
5. **`users/{uid}/replies`** — start writing once `track` polls produce data. No file to migrate.

For each: a one-off migration script under `migrations/0NN_<name>.py` that reads the file and bulk-writes to Firestore. Idempotent (re-runnable without dupes).

---

## Open / TBD

- **Production auth**: needs decision before deploy (SA key with org policy lifted, vs Workload Identity Federation)
- **Budget alert**: $5/month threshold on Cloud Console (recommended in earlier convo)
- **Backup / PITR**: not enabled (Spark plan; would need Blaze + extra cost). Acceptable since data is replayable from logs
- **Index creation**: composite indexes will be prompted by failed queries on first run; document them here as they emerge
