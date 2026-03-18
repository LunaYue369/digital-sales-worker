"""Track Pipeline — per-user background reply polling + GPT analysis + Slack notification."""

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from core import state
from services import reply_tracker
from agents import reply_analyzer

log = logging.getLogger(__name__)

POLL_INTERVAL = int(os.getenv("REPLY_POLL_INTERVAL", "60"))
MAX_ANALYZE_WORKERS = int(os.getenv("TRACK_MAX_WORKERS", "5"))


def run_track_pipeline(user_id: str, say):
    say(f"\U0001f50d *Reply tracking started*\nPolling every {POLL_INTERVAL}s \u2022 Use `/ stop track` to stop")

    while state.is_tracking(user_id):
        try:
            new_replies = reply_tracker.check_replies(user_id)

            if new_replies:
                _process_replies(user_id, new_replies, say)

        except Exception as e:
            log.error("Tracking replies error: %s", e, exc_info=True)

        for _ in range(POLL_INTERVAL):
            if not state.is_tracking(user_id):
                break
            time.sleep(1)

    say("\u23f9\ufe0f *Reply tracking stopped.*")


def _process_replies(user_id: str, new_replies: list[dict], say):
    human_replies = []
    for reply in new_replies:
        rtype = reply["reply_type"]
        company = reply.get("company_name", "?")
        email = reply.get("contact_email", "?")

        if rtype == "human":
            human_replies.append(reply)
        elif rtype == "bounce":
            say(f"\u26a0\ufe0f *Bounce* \u2022 `{email}` \u2022 {company}")
        elif rtype == "ooo":
            say(f"\U0001f3d6\ufe0f *Out-of-Office* \u2022 `{email}` \u2022 {company}")
        elif rtype == "spam_auto":
            say(f"\U0001f6ab *Spam Notification* \u2022 `{email}` \u2022 {company}")
        elif rtype == "auto_reply":
            say(f"\U0001f916 *Auto-Reply* \u2022 `{email}` \u2022 {company}")

    if not human_replies:
        return

    with ThreadPoolExecutor(max_workers=MAX_ANALYZE_WORKERS) as pool:
        futures = {
            pool.submit(_analyze_reply, user_id, reply): reply
            for reply in human_replies
        }
        for future in as_completed(futures):
            reply = futures[future]
            try:
                analysis = future.result()
            except Exception as e:
                log.error("Reply analysis failed for %s: %s",
                          reply.get("company_name", "?"), e)
                analysis = {}
            _notify_human_reply(reply, analysis, say)


def _analyze_reply(user_id: str, reply_record: dict) -> dict:
    analysis = reply_analyzer.analyze_reply(reply_record, user_id)
    reply_tracker.update_reply_analysis(user_id, reply_record["reply_message_id"], analysis)
    return analysis


_SENTIMENT_LABELS = {
    "interested": "Positive - prospect is interested",
    "rejected": "Negative - prospect declined",
    "neutral": "Neutral - no clear signal",
}

_INTENT_LABELS = {
    "interested": "Wants to learn more or schedule a call",
    "not_interested": "Explicitly not interested",
    "asking_question": "Has specific questions about the product",
    "requesting_info": "Wants pricing, demos, or materials",
    "referring_to_colleague": "Redirecting to someone else",
    "unsubscribe": "Wants to be removed from mailing",
    "complaint": "Negative feedback about the outreach",
    "wrong_person": "Not the right contact",
    "just_acknowledging": "Just acknowledging receipt",
    "other": "Other",
}

_SENTIMENT_EMOJI = {
    "interested": "\U0001f7e2",   # green circle
    "rejected": "\U0001f534",     # red circle
    "neutral": "\U0001f7e1",      # yellow circle
}


def _notify_human_reply(reply_record: dict, analysis: dict, say):
    company = reply_record.get("company_name", "?")
    from_addr = reply_record.get("reply_from", "?")
    industry = reply_record.get("industry", "?")

    sentiment = analysis.get("sentiment", "?")
    intent = analysis.get("intent", "?")
    reason = analysis.get("reason_summary", "")
    why = analysis.get("why_accepted_or_rejected", "")
    follow_up = analysis.get("follow_up_advice", "")
    tip = analysis.get("improvement_tip", "")
    factors = analysis.get("company_factors", {})

    emoji = _SENTIMENT_EMOJI.get(sentiment, "\u2753")
    sentiment_label = _SENTIMENT_LABELS.get(sentiment, sentiment)
    intent_label = _INTENT_LABELS.get(intent, intent)

    # Header
    msg = f"{emoji} *New Reply: {company}* ({industry})\n\n"

    # Basic info
    msg += f"\U0001f4e8 *From:* {from_addr}\n"
    msg += f"\u23f1\ufe0f *Response time:* {reply_record.get('time_to_reply_hours', '?')} hours\n\n"

    # Sentiment & Intent (separate lines, human-readable)
    msg += f"\U0001f3af *Sentiment:* {sentiment_label}\n"
    msg += f"\U0001f4ac *Intent:* {intent_label}\n\n"

    # Summary & Analysis
    if reason:
        msg += f"\U0001f4cb *Summary:*\n{reason}\n\n"
    if why:
        msg += f"\U0001f50d *Analysis:*\n{why}\n\n"

    # Company profile
    if factors:
        parts = []
        if factors.get("size_signal") and factors["size_signal"] != "unknown":
            parts.append(f"Size: {factors['size_signal']}")
        if factors.get("pain_point"):
            parts.append(f"Pain point: {factors['pain_point']}")
        if factors.get("current_solution"):
            parts.append(f"Current solution: {factors['current_solution']}")
        if parts:
            msg += f"\U0001f3e2 *Company Profile:*\n" + "\n".join(f"  \u2022 {p}" for p in parts) + "\n\n"

    # Follow-up advice
    if follow_up:
        msg += f"\U0001f4a1 *Follow-up Advice:*\n{follow_up}\n\n"

    # Improvement tip
    if tip:
        msg += f"\U0001f4dd *Improvement Tip:*\n{tip}\n\n"

    # Original reply text
    # Use > quote block instead of _italic_ to avoid Slack markdown breaking on multiline
    reply_body = reply_record.get("reply_body", "")[:300]
    quoted = "\n".join(f"> {line}" for line in reply_body.split("\n"))
    msg += f"---\n\U0001f4e9 *Original Reply:*\n{quoted}"

    say(msg)
