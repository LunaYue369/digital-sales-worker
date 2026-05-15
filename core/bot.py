import logging
import re
import threading

from core import state
from core.user_config import is_registered, is_admin, get_user_name, list_templates
from auto.auto_pipeline import run_auto_pipeline
from auto.track_pipeline import run_track_pipeline
from auto.prospect_pipeline import run_prospect
from auto.report_pipeline import run_report, run_insights
from services import usage_tracker, email_sender, reply_tracker
from services.auth import (
    needs_auth, has_pending_flow, generate_auth_url, exchange_auth_code,
)


log = logging.getLogger(__name__)

KNOWN_COMMANDS = {"auth", "auto", "templates", "track", "report", "insights",
                  "prospect", "usage", "help", "stop", "status"}


def handle_message(event: dict, say, client=None):
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

    tokens = text.split()
    lower_tokens = [t.lower() for t in tokens]
    cmd = lower_tokens[0] if lower_tokens else ""
    sub = lower_tokens[1] if len(lower_tokens) > 1 else ""

    # If user is mid auth flow, treat unrecognized messages as the OAuth code paste
    if has_pending_flow(user_id) and cmd not in KNOWN_COMMANDS:
        _handle_auth_code(user_id, text, say)
        return

    if cmd == "auth":
        _handle_auth(user_id, say)
        return

    # auto
    if cmd == "stop" and sub == "auto":
        _handle_stop_auto(user_id, say)
    elif cmd == "status" and sub == "auto":
        _handle_status_auto(user_id, say)
    elif cmd == "auto":
        channel = event.get("channel", "")
        _handle_auto(user_id, say, client, channel)
    # templates
    elif cmd == "templates":
        _handle_templates(user_id, say)
    # track
    elif cmd == "stop" and sub == "track":
        _handle_stop_track(user_id, say)
    elif cmd == "status" and sub == "track":
        _handle_status_track(user_id, say)
    elif cmd == "track":
        _handle_track(user_id, say)
    # report
    elif cmd == "report":
        run_report(user_id, say)
    # insights
    elif cmd == "insights":
        run_insights(user_id, say)
    # prospect
    elif cmd == "prospect" and len(tokens) > 1:
        _handle_prospect(user_id, text, say)
    elif cmd == "prospect":
        _handle_prospect_help(say)
    # usage
    elif cmd == "usage":
        _handle_usage(user_id, text, say)
    # help
    elif cmd == "help":
        _handle_help(say)


# ── Auth ───────────────────────────────────────────────────────────

def _handle_auth(user_id: str, say):
    if not needs_auth(user_id):
        say("You are already authorized. To re-authorize, delete your token and run `auth` again.")
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
        f"1️⃣ Click the link below and sign in with your Gmail:\n"
        f"{auth_url}\n\n"
        f"2️⃣ After authorizing, the page will show *\"This site can’t be reached\"* — this is normal!\n\n"
        f"3️⃣ Copy the *entire URL* from your browser address bar (it will look like `http://localhost/?code=...&scope=...`) and *paste it here* — I'll pull out the code automatically."
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
        say(f"Authorization failed: {e}\nPlease run `auth` to try again.")
        return

    name = get_user_name(user_id)
    say(f"Gmail authorized successfully for *{name}*! You can now use `auto` and `track`.")


# ── Auto ───────────────────────────────────────────────────────────

def _handle_auto(user_id: str, say, client=None, channel: str = ""):
    if state.is_auto_running(user_id):
        say("Auto mode is already running. Use `stop auto` to stop it.")
        return
    available = list_templates(user_id)
    if not available:
        say("You have no templates configured. Ask an admin to add one in `config/users.json`.")
        return
    state.start_auto(user_id)
    t = threading.Thread(
        target=run_auto_pipeline,
        args=(user_id, say, client, channel),
        daemon=True,
    )
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
        say("Reply tracking is already running. Use `stop track` to stop it.")
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
    rest = re.sub(r"^usage\s*", "", text, flags=re.IGNORECASE).strip()
    log.info("Usage command: user=%s, text=%r, rest=%r, is_admin=%s",
             user_id, text, rest, is_admin(user_id))

    # Admin: `usage` with no args → show all users' usage
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
    rest = re.sub(r"^prospect\s+", "", text, flags=re.IGNORECASE).strip()
    if not rest:
        _handle_prospect_help(say)
        return

    # Extract flags first, then what's left is the query
    depth = None
    depth_match = re.search(r"--depth\s+(\d+)", rest)
    if depth_match:
        depth = int(depth_match.group(1))
        rest = rest[:depth_match.start()] + rest[depth_match.end():]

    template = None
    tpl_match = re.search(r"--template\s+([A-Za-z0-9_-]+)", rest)
    if tpl_match:
        template = tpl_match.group(1)
        rest = rest[:tpl_match.start()] + rest[tpl_match.end():]

    debug = bool(re.search(r"--debug\b", rest))
    rest = re.sub(r"--debug\b", "", rest)

    rest = rest.strip().strip("|").strip()

    queries = [q.strip() for q in rest.split("|") if q.strip()]
    if not queries:
        _handle_prospect_help(say)
        return

    if template:
        available = list_templates(user_id)
        if template not in available:
            say(f"Unknown template `{template}`. Your templates: {', '.join(f'`{t}`' for t in available)}\n"
                f"Use `templates` to see details.")
            return

    t = threading.Thread(target=run_prospect, args=(user_id, queries, say, depth, debug, template), daemon=True)
    t.start()


def _handle_prospect_help(say):
    say("*Prospect — Google Maps Lead Finder*\n"
        "Scrape Google Maps for businesses, find emails, upload CSV to Drive.\n"
        "*Does NOT send emails* — only generates leads.\n"
        "Auto-dedup: skips companies already prospected or contacted.\n\n"
        "*Usage:*\n"
        "`prospect dental clinic in Los Angeles, CA`\n"
        "`prospect dental clinic in LA, CA | auto repair in Houston, TX`\n"
        "`prospect HVAC contractors in Anaheim --template comprehensive`\n"
        "`prospect dental clinic in LA, CA --depth 5 --debug`\n\n"
        "*Options:*\n"
        "`--template <name>` — *required.* Tag the output CSV so `auto` routes it to that template.\n"
        "    Filename becomes `prospect_<ts>__<template>.csv`. `auto` reads the suffix to pick a template.\n"
        "    CSVs without a `__<template>` tag are skipped — re-run prospect if you forgot.\n"
        "`--depth N` — scraper scroll depth (default from .env)\n"
        "`--debug` — show browser window (headful mode)\n\n"
        "*Note:* Use `|` to separate multiple searches. Commas are fine in city names.\n\n"
        "*Tip:* Use different area names to cover more ground:\n"
        "`prospect dental clinic in Santa Monica, CA`\n"
        "`prospect dental clinic in Pasadena`\n\n"
        "After CSV appears in Drive, use `auto` to start sending.")


def _handle_templates(user_id: str, say):
    """List the user's email templates with type + subject preview."""
    from core.user_config import get_template_config
    names = list_templates(user_id)
    lines = [f":scroll: *Your Email Templates* ({len(names)} total)\n"]
    for name in names:
        tpl = get_template_config(user_id, name)
        if tpl.get("static_body"):
            subj = tpl.get("static_subject", "(no subject)")
            lines.append(f"• `{name}` — *static* · subject: _{subj}_")
        else:
            lines.append(f"• `{name}` — *AI-generated* (research + write + review)")
    lines.append(
        "\n:bulb: Templates are picked from the CSV filename suffix (`__<name>` before the extension). "
        "Use `prospect ... --template <name>` so the CSV gets tagged. Just run `auto` to dispatch all tagged CSVs in Drive. "
        "To add a new template, edit `config/users.json` → add an entry under your `templates` key. "
        "For AI templates you can also drop `config/<you>/copywriters/<name>.md` "
        "to customize voice/style for that template."
    )
    say("\n".join(lines))


def _handle_help(say):
    say(
        ":robot_face: *Digital Sales Bot — Commands*\n\n"
        ":envelope: *Email Campaigns*\n"
        "`auto` — Start AI-powered email campaign. Scans your Drive folder, picks each CSV's template from its filename suffix (`__<template>`).\n"
        "`templates` — List your available email templates\n"
        "`stop auto` — Stop the current campaign\n"
        "`status auto` — Check campaign status\n\n"
        ":mag: *Lead Generation*\n"
        "`prospect <query>` — Scrape Google Maps for leads (e.g. `prospect HVAC in LA, CA`)\n\n"
        ":incoming_envelope: *Reply Tracking*\n"
        "`track` — Start monitoring for email replies\n"
        "`stop track` — Stop reply tracking\n"
        "`status track` — Check tracking status\n\n"
        ":bar_chart: *Reports*\n"
        "`report` — Campaign performance report\n"
        "`insights` — AI-generated insights from reply data\n"
        "`usage` — Token usage and cost summary\n\n"
        ":key: *Setup*\n"
        "`auth` — Authorize your Gmail account\n"
        "`help` — Show this message\n\n"
        ":bulb: *Templates:* Each prospect run tags its CSV with `--template <name>`. `auto` reads the suffix and dispatches each file to its tagged template. "
        "Run `templates` to see what's available. Ask your admin to set up new ones."
    )
