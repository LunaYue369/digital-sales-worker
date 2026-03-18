import logging
import re
import threading

from core import state
from core.user_config import is_registered, is_admin, get_user_name
from auto.auto_pipeline import run_auto_pipeline
from auto.track_pipeline import run_track_pipeline
from auto.prospect_pipeline import run_prospect
from auto.report_pipeline import run_report, run_insights
from services import usage_tracker, email_sender, reply_tracker
from services.auth import (
    needs_auth, has_pending_flow, generate_auth_url, exchange_auth_code,
)


log = logging.getLogger(__name__)


def handle_message(event: dict, say):
    text = (event.get("text") or "").strip()

    if event.get("bot_id") or event.get("subtype") == "bot_message":
        return

    # Extract user_id from Slack event
    user_id = event.get("user", "")
    if not user_id:
        return

    # Strip @mention prefix
    text = re.sub(r"<@[A-Z0-9]+>\s*", "", text).strip()

    # Check if user is registered
    if not is_registered(user_id):
        say(f"You are not registered. Ask an admin to add your Slack user ID (`{user_id}`) to `data/users.json`.")
        return

    lower = text.lower()

    # auth — handle pending auth code (user pasting the OAuth code)
    if has_pending_flow(user_id) and not lower.startswith("/"):
        _handle_auth_code(user_id, text, say)
        return

    # auth
    if lower.startswith("/ auth"):
        _handle_auth(user_id, say)
        return

    # auto
    if lower.startswith("/ stop auto"):
        _handle_stop_auto(user_id, say)
    elif lower.startswith("/ status auto"):
        _handle_status_auto(user_id, say)
    elif lower.startswith("/ auto"):
        _handle_auto(user_id, say)
    # track
    elif lower.startswith("/ stop track"):
        _handle_stop_track(user_id, say)
    elif lower.startswith("/ status track"):
        _handle_status_track(user_id, say)
    elif lower.startswith("/ track"):
        _handle_track(user_id, say)
    # report
    elif lower.startswith("/ report"):
        run_report(user_id, say)
    # insights
    elif lower.startswith("/ insights"):
        run_insights(user_id, say)
    # prospect
    elif lower.startswith("/ prospect "):
        _handle_prospect(user_id, text, say)
    elif lower == "/ prospect":
        _handle_prospect_help(say)
    # usage
    elif lower.startswith("/ usage"):
        _handle_usage(user_id, text, say)


# ── Auth ───────────────────────────────────────────────────────────

def _handle_auth(user_id: str, say):
    if not needs_auth(user_id):
        say("You are already authorized. To re-authorize, delete your token and run `/ auth` again.")
        return

    try:
        auth_url = generate_auth_url(user_id)
    except Exception as e:
        log.error("Failed to generate auth URL for %s: %s", user_id, e)
        say(f"Failed to start auth flow: {e}")
        return

    name = get_user_name(user_id)
    say(
        f"\U0001f510 *Gmail Authorization for {name}*\n\n"
        f"1\ufe0f\u20e3 Click the link below and sign in with your Gmail:\n"
        f"{auth_url}\n\n"
        f"2\ufe0f\u20e3 After authorizing, the page will show *\"This site can\u2019t be reached\"* — this is normal!\n\n"
        f"3\ufe0f\u20e3 Look at your browser *address bar*, it will look like:\n"
        f"`http://localhost/?code=4/0AXXXX...&scope=...`\n\n"
        f"4\ufe0f\u20e3 Copy everything between `code=` and `&scope` and *paste it here*."
    )


def _handle_auth_code(user_id: str, code: str, say):
    code = code.strip()
    if not code:
        return

    # User might paste the full URL or "code=XXXX&scope=..."
    if "code=" in code:
        code = code.split("code=")[-1].split("&")[0]
    code = code.strip()

    try:
        exchange_auth_code(user_id, code)
    except Exception as e:
        log.error("Auth code exchange failed for %s: %s", user_id, e)
        say(f"Authorization failed: {e}\nPlease run `/ auth` to try again.")
        return

    name = get_user_name(user_id)
    say(f"Gmail authorized successfully for *{name}*! You can now use `/ auto` and `/ track`.")


# ── Auto ───────────────────────────────────────────────────────────

def _handle_auto(user_id: str, say):
    if state.is_auto_running(user_id):
        say("Auto mode is already running. Use `/ stop auto` to stop it.")
        return
    state.start_auto(user_id)
    t = threading.Thread(target=run_auto_pipeline, args=(user_id, say), daemon=True)
    state.set_auto_thread(user_id, t)
    t.start()


def _handle_stop_auto(user_id: str, say):
    if not state.is_auto_running(user_id):
        say("Auto mode is not running.")
        return
    state.stop_auto(user_id)
    say("Stopping auto mode... (will stop after current operation completes)")


def _handle_status_auto(user_id: str, say):
    running = state.is_auto_running(user_id)
    sent_count = email_sender.get_sent_count(user_id)
    say(f"*Auto Status*\n"
        f"Auto mode: {'RUNNING' if running else 'STOPPED'}\n"
        f"Total emails sent: {sent_count}")


# ── Track ──────────────────────────────────────────────────────────

def _handle_track(user_id: str, say):
    if state.is_tracking(user_id):
        say("Reply tracking is already running. Use `/ stop track` to stop it.")
        return
    state.start_tracking(user_id)
    t = threading.Thread(target=run_track_pipeline, args=(user_id, say), daemon=True)
    state.set_tracking_thread(user_id, t)
    t.start()


def _handle_stop_track(user_id: str, say):
    if not state.is_tracking(user_id):
        say("Reply tracking is not running.")
        return
    state.stop_tracking(user_id)
    say("Stopping reply tracking...")


def _handle_status_track(user_id: str, say):
    tracking = state.is_tracking(user_id)
    reply_log = reply_tracker.get_reply_log(user_id)
    human = sum(1 for r in reply_log if r["reply_type"] == "human")
    bounces = sum(1 for r in reply_log if r["reply_type"] == "bounce")
    ooo = sum(1 for r in reply_log if r["reply_type"] == "ooo")
    spam = sum(1 for r in reply_log if r["reply_type"] == "spam_auto")
    auto = sum(1 for r in reply_log if r["reply_type"] == "auto_reply")

    analyzed = [r for r in reply_log if r["reply_type"] == "human" and r.get("analysis")]
    interested = sum(1 for r in analyzed if r["analysis"].get("sentiment") == "interested")
    rejected = sum(1 for r in analyzed if r["analysis"].get("sentiment") == "rejected")
    neutral = sum(1 for r in analyzed if r["analysis"].get("sentiment") == "neutral")

    say(f"*Track Status*\n"
        f"Tracking: {'RUNNING' if tracking else 'STOPPED'}\n"
        f"First replies: {human} human | {bounces} bounces | {ooo} OOO | {spam} spam | {auto} auto-reply\n"
        f"Sentiment: {interested} interested | {rejected} rejected | {neutral} neutral")


# ── Usage ───────────────────────────────────────────────────────────

def _handle_usage(user_id: str, text: str, say):
    rest = re.sub(r"^/\s*usage\s*", "", text, flags=re.IGNORECASE).strip()
    log.info("Usage command: user=%s, text=%r, rest=%r, is_admin=%s",
             user_id, text, rest, is_admin(user_id))

    # Admin: `/ usage` with no args → show all users' usage
    if not rest and is_admin(user_id):
        say(usage_tracker.format_all_users_slack_report())
        return

    if rest:
        report = usage_tracker.format_slack_report(user_id, rest)
        say(report)
        return

    sent_count = email_sender.get_sent_count(user_id)
    reply_log = reply_tracker.get_reply_log(user_id)
    reply_count = sum(1 for r in reply_log if r["reply_type"] == "human")
    say(usage_tracker.format_full_slack_report(user_id, sent_count, reply_count))


# ── Prospect ──────────────────────────────────────────────────────────

def _handle_prospect(user_id: str, text: str, say):
    rest = re.sub(r"^/\s*prospect\s+", "", text, flags=re.IGNORECASE).strip()
    if not rest:
        _handle_prospect_help(say)
        return

    # Extract flags first, then what's left is the query
    depth = None
    depth_match = re.search(r"--depth\s+(\d+)", rest)
    if depth_match:
        depth = int(depth_match.group(1))
        rest = rest[:depth_match.start()] + rest[depth_match.end():]

    debug = bool(re.search(r"--debug\b", rest))
    rest = re.sub(r"--debug\b", "", rest)

    rest = rest.strip().strip("|").strip()

    queries = [q.strip() for q in rest.split("|") if q.strip()]
    if not queries:
        _handle_prospect_help(say)
        return
    t = threading.Thread(target=run_prospect, args=(user_id, queries, say, depth, debug), daemon=True)
    t.start()


def _handle_prospect_help(say):
    say("*Prospect — Google Maps Lead Finder*\n"
        "Scrape Google Maps for businesses, find emails, upload CSV to Drive.\n"
        "*Does NOT send emails* — only generates leads.\n"
        "Auto-dedup: skips companies already prospected or contacted.\n\n"
        "*Usage:*\n"
        "`/ prospect dental clinic in Los Angeles, CA`\n"
        "`/ prospect dental clinic in LA, CA | auto repair in Houston, TX`\n"
        "`/ prospect dental clinic in LA, CA --depth 5 --debug`\n\n"
        "*Options:*\n"
        "`--depth N` — scraper scroll depth (default from .env)\n"
        "`--debug` — show browser window (headful mode)\n\n"
        "*Note:* Use `|` to separate multiple searches. Commas are fine in city names.\n\n"
        "*Tip:* Use different area names to cover more ground:\n"
        "`/ prospect dental clinic in Santa Monica, CA`\n"
        "`/ prospect dental clinic in Pasadena`\n\n"
        "After CSV appears in Drive, use `/ auto` to start sending.")
