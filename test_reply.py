"""Test Reply Script — batch reply to sent test emails using Gmail API.

Reads sent_log.json, finds all test emails (luna+xxx@gmic.ai),
and replies to each thread with random interested/rejected/neutral content.

Usage:
    python test_reply.py              # reply to all unreplied test emails
    python test_reply.py --dry-run    # show what would be sent without sending
    python test_reply.py --limit 5    # only reply to first 5 unreplied
"""

import argparse
import base64
import json
import logging
import os
import random
import sys
import time
from email.mime.text import MIMEText

# Add project root to path so we can import services
sys.path.insert(0, os.path.dirname(__file__))

from services.auth import get_gmail_service

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

SENT_LOG_PATH = os.path.join(os.path.dirname(__file__), "data", "sent_log.json")
REPLY_LOG_PATH = os.path.join(os.path.dirname(__file__), "data", "reply_log.json")

# ── Reply templates ────────────────────────────────────────────────────
# Each has a sentiment label (for verification) and a body template.
# {company} and {product} are replaced at runtime.

REPLY_TEMPLATES = {
    "interested": [
        "Hi Nate,\n\nThanks for reaching out. This sounds interesting — we've been looking for a better phone answering solution. Could you send over some pricing details? We currently have about 50 missed calls per week after hours.\n\nBest,\nTest User",
        "Hey Nate,\n\nI saw your email about Telalive. We actually just lost a big client because we missed their after-hours call. Would love to set up a demo. Are you available this week?\n\nCheers,\nTest User",
        "Nate,\n\nGood timing on this. We've been paying $3,000/month for a live answering service and it's not great. $39/month sounds too good to be true — what's the catch? Let's chat.\n\nThanks,\nTest User",
        "Hi there,\n\nI'm definitely interested in learning more about Telalive. We're a small team and can't always answer the phone. Can you tell me more about how the AI handles complex questions from callers?\n\nRegards,\nTest User",
        "Hello Nate,\n\nI've been meaning to find an AI solution for our phone lines. We get a lot of appointment requests after 5pm. Does Telalive integrate with Google Calendar or any scheduling tools?\n\nLooking forward to hearing from you,\nTest User",
    ],
    "rejected": [
        "Hi,\n\nThanks but we're not interested. We already use Ruby Receptionists and are happy with them. Please remove me from your mailing list.\n\nRegards,\nTest User",
        "Nate,\n\nI appreciate the outreach but this isn't relevant to our business. We don't take phone calls — everything is done through our online portal.\n\nBest,\nTest User",
        "Not interested. We handle all calls in-house and have no plans to change that.\n\nDo not email me again.",
        "Hi Nate,\n\nWe actually looked into AI phone solutions last year and decided against it. Our customers prefer speaking with a real person, and we had bad experiences with automated systems sounding robotic.\n\nNo thanks,\nTest User",
        "Hey,\n\nWe're a two-person shop and we literally get maybe 3 calls a day. This is way overkill for us. Also $39/month for something we barely need isn't worth it.\n\nThanks anyway,\nTest User",
    ],
    "neutral": [
        "Hi Nate,\n\nThanks for your email. I'll forward this to our operations manager — she handles our phone systems. Her name is Sarah Chen.\n\nBest,\nTest User",
        "Got it, thanks.",
        "Hi,\n\nInteresting concept. I need to discuss this with my team first. Can you send me some case studies or testimonials from businesses similar to ours?\n\nThanks,\nTest User",
        "Nate,\n\nI'm not sure if this is the right fit but I'm not saying no either. What kind of businesses typically use Telalive? Do you have experience with our industry specifically?\n\nRegards,\nTest User",
        "Thanks for the info. We might revisit this next quarter when our budget cycle resets. I'll keep your details on file.\n\nBest,\nTest User",
    ],
}

# Distribution: ~40% interested, ~30% rejected, ~30% neutral
SENTIMENT_WEIGHTS = {
    "interested": 0.4,
    "rejected": 0.3,
    "neutral": 0.3,
}


def _load_sent_log() -> list[dict]:
    if os.path.exists(SENT_LOG_PATH):
        with open(SENT_LOG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def _load_reply_log() -> list[dict]:
    if os.path.exists(REPLY_LOG_PATH):
        with open(REPLY_LOG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def _get_replied_thread_ids() -> set[str]:
    """Get thread_ids that already have replies in reply_log."""
    reply_log = _load_reply_log()
    return {r["thread_id"] for r in reply_log if r.get("thread_id")}


def _get_test_emails_to_reply(limit: int = 0) -> list[dict]:
    """Find sent test emails (luna+xxx@gmic.ai) that haven't been replied to yet."""
    sent_log = _load_sent_log()
    replied_threads = _get_replied_thread_ids()

    candidates = []
    for s in sent_log:
        if s["status"] != "sent":
            continue
        if not s.get("thread_id"):
            continue
        # Only test emails (luna+xxx)
        email = s.get("contact_email", "")
        if not email.startswith("luna+"):
            continue
        # Skip already replied
        if s["thread_id"] in replied_threads:
            continue
        candidates.append(s)

    if limit > 0:
        candidates = candidates[:limit]
    return candidates


def _pick_reply(company_name: str) -> tuple[str, str]:
    """Pick a random sentiment and reply template. Returns (sentiment, body)."""
    sentiments = list(SENTIMENT_WEIGHTS.keys())
    weights = list(SENTIMENT_WEIGHTS.values())
    sentiment = random.choices(sentiments, weights=weights, k=1)[0]
    body = random.choice(REPLY_TEMPLATES[sentiment])
    return sentiment, body


def _send_reply(gmail, thread_id: str, original_subject: str, body: str) -> dict | None:
    """Send a reply to an existing thread via Gmail API."""
    try:
        msg = MIMEText(body, "plain", "utf-8")
        # Reply to self
        msg["to"] = "luna@gmic.ai"
        # Gmail uses Re: prefix to keep in the same thread
        subject = original_subject
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"
        msg["subject"] = subject

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        result = gmail.users().messages().send(
            userId="me",
            body={"raw": raw, "threadId": thread_id},
        ).execute()
        return result
    except Exception as e:
        log.error("Failed to reply to thread %s: %s", thread_id, e)
        return None


def main():
    parser = argparse.ArgumentParser(description="Batch reply to test emails")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be sent without sending")
    parser.add_argument("--limit", type=int, default=0, help="Max number of replies to send (0 = all)")
    args = parser.parse_args()

    candidates = _get_test_emails_to_reply(args.limit)

    if not candidates:
        log.info("No unreplied test emails found in sent_log.")
        return

    log.info("Found %d unreplied test emails", len(candidates))

    gmail = None if args.dry_run else get_gmail_service()

    stats = {"interested": 0, "rejected": 0, "neutral": 0, "sent": 0, "failed": 0}

    for s in candidates:
        company = s.get("company_name", "?")
        thread_id = s["thread_id"]
        subject = s.get("subject", "")
        sentiment, body = _pick_reply(company)
        stats[sentiment] += 1

        if args.dry_run:
            log.info("[DRY RUN] Would reply to %s (%s) — sentiment: %s", company, thread_id[:12], sentiment)
            continue

        result = _send_reply(gmail, thread_id, subject, body)
        if result:
            stats["sent"] += 1
            log.info("Replied to %s — sentiment: %s (thread: %s)", company, sentiment, thread_id[:12])
        else:
            stats["failed"] += 1
            log.error("Failed reply to %s", company)

        # Small delay to avoid Gmail rate limits
        time.sleep(1)

    log.info("")
    log.info("=== Summary ===")
    log.info("Total: %d | Sent: %d | Failed: %d", len(candidates), stats["sent"], stats["failed"])
    log.info("Sentiment mix: %d interested / %d rejected / %d neutral",
             stats["interested"], stats["rejected"], stats["neutral"])


if __name__ == "__main__":
    main()
