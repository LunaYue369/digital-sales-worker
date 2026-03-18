"""Auto Mode pipeline — orchestrates the full flow in a background thread. Per-user."""

import logging
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

from core import state
from core.user_config import get_drive_folder_id
from services import drive_poller, spreadsheet, email_sender, usage_tracker, failed_log
from agents import researcher, copywriter, reviewer

log = logging.getLogger(__name__)

POLL_INTERVAL = int(os.getenv("AUTO_POLL_INTERVAL", "300"))
MAX_REVIEW_ROUNDS = int(os.getenv("REVIEWER_MAX_ROUNDS", "3"))
MAX_WORKERS = int(os.getenv("PIPELINE_MAX_WORKERS", "5"))
MAX_RETRIES_PER_COMPANY = int(os.getenv("PIPELINE_RETRY_LIMIT", "3"))


def run_auto_pipeline(user_id: str, say):
    folder_id = get_drive_folder_id(user_id)
    say(f"Auto mode started. Polling Google Drive every {POLL_INTERVAL}s for new files...\n"
        f"Drive folder: `{folder_id or 'NOT SET'}`")

    first_run = True
    while state.is_auto_running(user_id):
        try:
            found = _poll_once(user_id, say)
            if first_run and not found:
                say("First poll complete — no new files found. Waiting for uploads...")
            first_run = False
        except Exception as e:
            log.error("Pipeline error: %s", e, exc_info=True)
            say(f"Pipeline error: {e}")

        for _ in range(POLL_INTERVAL):
            if not state.is_auto_running(user_id):
                break
            time.sleep(1)

    say("Auto mode stopped.")


def _poll_once(user_id: str, say) -> bool:
    new_files = drive_poller.poll_new_files(user_id)

    if not new_files:
        return False

    for file_info in new_files:
        if not state.is_auto_running(user_id):
            break
        file_name = file_info["name"]
        file_id = file_info["id"]
        say(f"New file detected: `{file_name}` — processing...")

        try:
            _process_file(user_id, file_info, say)
            drive_poller.mark_processed(user_id, file_id)
        except Exception as e:
            log.error("Error processing %s: %s", file_name, e, exc_info=True)
            say(f"Error processing `{file_name}`: {e}")

    return True


def _process_file(user_id: str, file_info: dict, say):
    campaign_id = f"campaign_{uuid.uuid4().hex[:8]}"

    # 1. Download + parse
    df = drive_poller.download_file(user_id, file_info)
    companies = spreadsheet.parse_dataframe(user_id, df)
    if not companies:
        say(f"No new companies to process in `{file_info['name']}` (all already sent or missing required fields).")
        return

    say(f"Found {len(companies)} new companies in `{file_info['name']}`. Starting research...")

    # 2. Research (cache is shared across users)
    researched = researcher.research_batch(companies, campaign_id, user_id)
    cached = sum(1 for r in researched if r.get("from_cache"))
    say(f"*Research complete:* {len(researched)} companies ({cached} from cache)")

    if not state.is_auto_running(user_id):
        return

    # 3. Write & review (concurrent)
    say(f"Writing & reviewing {len(researched)} emails (up to {MAX_REVIEW_ROUNDS} rounds each, {MAX_WORKERS} concurrent)...")
    ready_to_send = []
    pending = list(researched)

    for attempt in range(1, MAX_RETRIES_PER_COMPANY + 1):
        if not pending:
            break

        if attempt > 1:
            say(f"Retry attempt {attempt}/{MAX_RETRIES_PER_COMPANY} for {len(pending)} failed companies (waiting 10s)...")
            time.sleep(10)

        still_failed = []

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {
                pool.submit(_write_and_review, company, campaign_id, user_id): company
                for company in pending
            }

            for future in as_completed(futures):
                if not state.is_auto_running(user_id):
                    pool.shutdown(wait=False, cancel_futures=True)
                    return

                company = futures[future]
                name = company.get("company_name", "?")
                try:
                    result = future.result()
                    ready_to_send.append(result)
                except Exception as e:
                    log.error("Failed for %s (attempt %d): %s", name, attempt, e)
                    still_failed.append(company)

                if len(ready_to_send) % 5 == 0 and ready_to_send:
                    say(f"  Progress: {len(ready_to_send)}/{len(researched)} emails done")

        pending = still_failed

    if pending:
        for c in pending:
            failed_log.record_error(user_id, c, campaign_id, f"Failed all {MAX_RETRIES_PER_COMPANY} retry attempts")
        names = [c.get("company_name", "?") for c in pending]
        say(f"Gave up on {len(pending)} companies after {MAX_RETRIES_PER_COMPANY} attempts: {', '.join(names)}")

    discarded = [e for e in ready_to_send if e.get("_review_failed")]
    approved = [e for e in ready_to_send if not e.get("_review_failed")]

    if discarded:
        for e in discarded:
            failed_log.record_discarded(user_id, e, campaign_id)
        names = [e.get("company_name", "?") for e in discarded]
        say(f"Discarded {len(discarded)} emails (failed all {MAX_REVIEW_ROUNDS} review rounds): {', '.join(names)}")
    if not approved:
        say("No emails passed review. Nothing to send.")
        return
    say(f"{len(approved)} emails passed review.")

    if not state.is_auto_running(user_id):
        return

    # 4. Send
    say(f"Sending {len(approved)} emails...")
    result = email_sender.send_campaign(user_id, approved, campaign_id)
    say(f"*Campaign `{campaign_id}` complete:*\n"
        f"  Sent: {result['sent']}\n"
        f"  Failed: {result['failed']}")

    # 5. Usage report
    report = usage_tracker.format_slack_report(user_id, campaign_id)
    say(report)


def _write_and_review(company: dict, campaign_id: str, user_id: str) -> dict:
    name = company.get("company_name", "?")
    subject, body, _ = copywriter.write_email(company, campaign_id, user_id)

    for round_num in range(1, MAX_REVIEW_ROUNDS + 1):
        review = reviewer.review_email(company, subject, body, campaign_id, user_id)
        if review.get("approved", False):
            log.info("%s APPROVED at round %d", name, round_num)
            break
        feedback = reviewer.build_feedback(review)
        log.info("%s REJECTED round %d", name, round_num)
        previous_email = f"Subject: {subject}\n\n{body}"
        subject, body, _ = copywriter.write_email(
            company, campaign_id, user_id, feedback=feedback, previous_email=previous_email
        )

    else:
        log.warning("%s FAILED all %d review rounds — discarding", name, MAX_REVIEW_ROUNDS)
        return {**company, "subject": subject, "body": body, "_review_failed": True}

    return {**company, "subject": subject, "body": body, "_review_failed": False}
