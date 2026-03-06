"""Slack message router — detects '/ auto', '/ stop', '/ usage' as plain text strings."""

import logging
import threading

from core import state
from auto.pipeline import run_auto_pipeline
from services import usage_tracker, email_sender

log = logging.getLogger(__name__)


def handle_message(event: dict, say):
    """Route incoming Slack messages to the appropriate handler."""
    text = (event.get("text") or "").strip()

    # Ignore bot's own messages
    if event.get("bot_id") or event.get("subtype") == "bot_message":
        return

    lower = text.lower()

    if lower.startswith("/ auto"):
        _handle_auto(say)
    elif lower.startswith("/ stop"):
        _handle_stop(say)
    elif lower.startswith("/ usage"):
        _handle_usage(text, say)
    elif lower.startswith("/ status"):
        _handle_status(say)
    # Ignore everything else — this bot only responds to commands


def _handle_auto(say):
    if state.is_running():
        say("Auto mode is already running. Use `/ stop` to stop it.")
        return

    state.start()
    t = threading.Thread(target=run_auto_pipeline, args=(say,), daemon=True)
    state.auto_thread = t
    t.start()


def _handle_stop(say):
    if not state.is_running():
        say("Auto mode is not running.")
        return
    state.stop()
    say("Stopping auto mode... (will stop after current operation completes)")


def _handle_usage(text: str, say):
    # "/ usage" — show all summary
    # "/ usage campaign_xxx" — show specific campaign
    # Strip the command prefix: "/ usage ..." or "/usage ..."
    import re
    rest = re.sub(r"^/\s*usage\s*", "", text, flags=re.IGNORECASE).strip()

    if rest:
        report = usage_tracker.format_slack_report(rest)
        say(report)
        return

    # All summary
    summary = usage_tracker.get_all_summary()
    sent_count = email_sender.get_sent_count()
    say(f"*Overall Usage Summary*\n"
        f"Total campaigns: {summary['total_campaigns']}\n"
        f"Total GPT calls: {summary['total_calls']}\n"
        f"Total tokens: {summary['total_tokens']:,}\n"
        f"Estimated cost: ${summary['total_cost']:.4f}\n"
        f"Total emails sent: {sent_count}")


def _handle_status(say):
    running = state.is_running()
    sent_count = email_sender.get_sent_count()
    say(f"*Status*\n"
        f"Auto mode: {'RUNNING' if running else 'STOPPED'}\n"
        f"Total emails sent: {sent_count}")
