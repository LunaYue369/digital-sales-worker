"""Auto Mode pipeline — orchestrates the full flow in a background thread. Per-user."""

import logging
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

from core import state
from core.user_config import get_drive_folder_id, get_template_config
from services import drive_poller, spreadsheet, email_sender, usage_tracker, failed_log
from agents import researcher, copywriter, reviewer
from slack_ui.blocks import build_progress_blocks, build_result_blocks

log = logging.getLogger(__name__)

POLL_INTERVAL = int(os.getenv("AUTO_POLL_INTERVAL", "300"))
MAX_REVIEW_ROUNDS = int(os.getenv("REVIEWER_MAX_ROUNDS", "3"))
MAX_WORKERS = int(os.getenv("PIPELINE_MAX_WORKERS", "5"))
MAX_RETRIES_PER_COMPANY = int(os.getenv("PIPELINE_RETRY_LIMIT", "3"))


def run_auto_pipeline(user_id: str, say, template: str = "default", client=None, channel: str = ""):
    folder_id = get_drive_folder_id(user_id)
    tpl_label = f" (template: `{template}`)" if template != "default" else ""
    say(f":file_folder: Auto mode started{tpl_label}. Polling Drive every {POLL_INTERVAL}s...\n"
        f"Drive folder: `{folder_id or 'NOT SET'}`")

    first_run = True
    while state.is_auto_running(user_id):
        try:
            found = _poll_once(user_id, say, template, client, channel)
            if first_run and not found:
                say(":hourglass_flowing_sand: First poll complete — no new files found. Waiting for uploads...")
            first_run = False
        except Exception as e:
            log.error("Pipeline error: %s", e, exc_info=True)
            say(f":x: Pipeline error: {e}")

        for _ in range(POLL_INTERVAL):
            if not state.is_auto_running(user_id):
                break
            time.sleep(1)

    say(":stop_sign: Auto mode stopped.")


def _poll_once(user_id: str, say, template: str = "default", client=None, channel: str = "") -> bool:
    new_files = drive_poller.poll_new_files(user_id)

    if not new_files:
        return False

    for file_info in new_files:
        if not state.is_auto_running(user_id):
            break
        file_name = file_info["name"]
        file_id = file_info["id"]
        say(f":page_facing_up: New file detected: `{file_name}`")

        try:
            _process_file(user_id, file_info, say, template, client, channel)
            drive_poller.mark_processed(user_id, file_id)
        except Exception as e:
            log.error("Error processing %s: %s", file_name, e, exc_info=True)
            say(f":x: Error processing `{file_name}`: {e}")

    return True


# ── Progress Helper ───────────────────────────────────────────────

class _ProgressTracker:
    """Manages a single Slack message that gets updated with progress."""

    def __init__(self, client, channel: str, say, template: str, is_static: bool, company_count: int):
        self._client = client
        self._channel = channel
        self._say = say
        self._template = template
        self._is_static = is_static
        self._company_count = company_count
        self._ts = None  # Slack message timestamp

    def update(self, stage: str, extra: str = ""):
        blocks = build_progress_blocks(
            current_stage=stage,
            is_static=self._is_static,
            template=self._template,
            extra=extra,
            company_count=self._company_count,
        )
        fallback = f"Campaign running — {stage}..."

        if self._client and self._channel:
            try:
                if self._ts is None:
                    resp = self._client.chat_postMessage(
                        channel=self._channel, text=fallback, blocks=blocks,
                    )
                    self._ts = resp["ts"]
                else:
                    self._client.chat_update(
                        channel=self._channel, ts=self._ts, text=fallback, blocks=blocks,
                    )
                return
            except Exception as e:
                log.warning("Block UI update failed, falling back to say(): %s", e)

        # Fallback: plain text via say()
        self._say(f"{stage}: {extra}" if extra else stage)

    def finish(self, sent: int, failed: int, campaign_id: str):
        blocks = build_result_blocks(
            sent=sent, failed=failed, campaign_id=campaign_id,
            template=self._template, is_static=self._is_static,
        )
        fallback = f"Campaign complete — sent {sent}, failed {failed}"

        if self._client and self._channel and self._ts:
            try:
                self._client.chat_update(
                    channel=self._channel, ts=self._ts, text=fallback, blocks=blocks,
                )
                return
            except Exception as e:
                log.warning("Block UI finish failed, falling back to say(): %s", e)

        self._say(fallback)


# ── Process File ──────────────────────────────────────────────────

def _process_file(user_id: str, file_info: dict, say, template: str = "default", client=None, channel: str = ""):
    campaign_id = f"campaign_{uuid.uuid4().hex[:8]}"

    # 1. Download + parse
    df = drive_poller.download_file(user_id, file_info)
    companies = spreadsheet.parse_dataframe(user_id, df)
    if not companies:
        say(f":warning: No new companies in `{file_info['name']}` (all already sent or missing required fields).")
        return

    # Check if static template
    tpl = get_template_config(user_id, template)
    is_static = bool(tpl.get("static_body"))

    # Initialize progress tracker
    progress = _ProgressTracker(client, channel, say, template, is_static, len(companies))
    progress.update("parse", f"Found {len(companies)} companies in `{file_info['name']}`")

    if is_static:
        # ── Static Template Pipeline ──────────────────────────────
        progress.update("generate", f"Generating {len(companies)} emails...")
        approved = []
        for i, company in enumerate(companies):
            if not state.is_auto_running(user_id):
                return
            subject, body, _ = copywriter.write_email(company, campaign_id, user_id, template=template)
            approved.append({**company, "subject": subject, "body": body})
            if (i + 1) % 50 == 0:
                progress.update("generate", f"Generated {i + 1}/{len(companies)} emails")

        progress.update("generate", f"{len(approved)} emails ready")

    else:
        # ── AI Template Pipeline ──────────────────────────────────
        # 2. Research
        progress.update("research", f"Researching {len(companies)} companies...")
        researched = researcher.research_batch(companies, campaign_id, user_id)
        cached = sum(1 for r in researched if r.get("from_cache"))
        progress.update("research", f"{len(researched)} researched ({cached} from cache)")

        if not state.is_auto_running(user_id):
            return

        # 3. Write & review
        progress.update("write", f"Writing {len(researched)} emails...")
        ready_to_send = []
        pending = list(researched)

        for attempt in range(1, MAX_RETRIES_PER_COMPANY + 1):
            if not pending:
                break

            if attempt > 1:
                time.sleep(10)

            still_failed = []

            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
                futures = {
                    pool.submit(_write_and_review, company, campaign_id, user_id, template): company
                    for company in pending
                }

                for future in as_completed(futures):
                    if not state.is_auto_running(user_id):
                        pool.shutdown(wait=False, cancel_futures=True)
                        return

                    company = futures[future]
                    try:
                        result = future.result()
                        ready_to_send.append(result)
                    except Exception as e:
                        log.error("Failed for %s (attempt %d): %s",
                                  company.get("company_name", "?"), attempt, e)
                        still_failed.append(company)

                    done = len(ready_to_send)
                    total = len(researched)
                    if done % 5 == 0 and done > 0:
                        stage = "review" if done > total * 0.5 else "write"
                        progress.update(stage, f"Progress: {done}/{total} emails")

            pending = still_failed

        if pending:
            for c in pending:
                failed_log.record_error(user_id, c, campaign_id, f"Failed all {MAX_RETRIES_PER_COMPANY} retry attempts")

        discarded = [e for e in ready_to_send if e.get("_review_failed")]
        approved = [e for e in ready_to_send if not e.get("_review_failed")]

        if discarded:
            for e in discarded:
                failed_log.record_discarded(user_id, e, campaign_id)

        if not approved:
            say(":warning: No emails passed review. Nothing to send.")
            return

        progress.update("review", f"{len(approved)} approved, {len(discarded)} discarded")

    if not state.is_auto_running(user_id):
        return

    # 4. Send
    progress.update("send", f"Sending {len(approved)} emails...")

    # Track send progress
    _sent_count = [0]
    _original_send = email_sender.send_email

    def _tracked_send(*args, **kwargs):
        result = _original_send(*args, **kwargs)
        _sent_count[0] += 1
        if _sent_count[0] % 20 == 0:
            progress.update("send", f"Sent {_sent_count[0]}/{len(approved)}")
        return result

    email_sender.send_email = _tracked_send
    try:
        result = email_sender.send_campaign(user_id, approved, campaign_id)
    finally:
        email_sender.send_email = _original_send

    # 5. Done
    progress.finish(result["sent"], result["failed"], campaign_id)

    # Usage report (separate message)
    report = usage_tracker.format_slack_report(user_id, campaign_id)
    if report and not is_static:
        say(report)


def _write_and_review(company: dict, campaign_id: str, user_id: str, template: str = "default") -> dict:
    name = company.get("company_name", "?")
    subject, body, _ = copywriter.write_email(company, campaign_id, user_id, template=template)

    for round_num in range(1, MAX_REVIEW_ROUNDS + 1):
        review = reviewer.review_email(company, subject, body, campaign_id, user_id)
        if review.get("approved", False):
            log.info("%s APPROVED at round %d", name, round_num)
            break
        feedback = reviewer.build_feedback(review)
        log.info("%s REJECTED round %d", name, round_num)
        previous_email = f"Subject: {subject}\n\n{body}"
        subject, body, _ = copywriter.write_email(
            company, campaign_id, user_id, feedback=feedback, previous_email=previous_email, template=template
        )

    else:
        log.warning("%s FAILED all %d review rounds — discarding", name, MAX_REVIEW_ROUNDS)
        return {**company, "subject": subject, "body": body, "_review_failed": True}

    return {**company, "subject": subject, "body": body, "_review_failed": False}
