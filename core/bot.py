import logging
import re
import threading

from core import state
from auto.auto_pipeline import run_auto_pipeline
from auto.track_pipeline import run_track_pipeline
from auto.prospect_pipeline import run_prospect
from auto.report_pipeline import run_report, run_insights
from services import usage_tracker, email_sender, reply_tracker


log = logging.getLogger(__name__)


# 用户给bot发不同的指令
def handle_message(event: dict, say):
    text = (event.get("text") or "").strip()

    # 忽略bot自己的消息，只检测用户发的
    if event.get("bot_id") or event.get("subtype") == "bot_message":
        return

    # Strip @mention prefix so commands parse correctly in channels
    text = re.sub(r"<@[A-Z0-9]+>\s*", "", text).strip()

    lower = text.lower()

    # auto: 开启 / 停止 / 状态
    if lower.startswith("/ stop auto"):
        _handle_stop_auto(say)
    elif lower.startswith("/ status auto"):
        _handle_status_auto(say)
    elif lower.startswith("/ auto"):
        _handle_auto(say)
    # track: 开启 / 停止 / 状态
    elif lower.startswith("/ stop track"):
        _handle_stop_track(say)
    elif lower.startswith("/ status track"):
        _handle_status_track(say)
    elif lower.startswith("/ track"):
        _handle_track(say)
    # report: 纯代码统计报告
    elif lower.startswith("/ report"):
        run_report(say)
    # insights: GPT 深度洞察
    elif lower.startswith("/ insights"):
        run_insights(say)
    # prospect: 找客户（只生成CSV，不发邮件）
    elif lower.startswith("/ prospect "):
        _handle_prospect(text, say)
    elif lower == "/ prospect":
        _handle_prospect_help(say)
    # usage: Token 用量
    elif lower.startswith("/ usage"):
        _handle_usage(text, say)


# auto pipeline
def _handle_auto(say):
    if state.is_auto_running():
        say("Auto mode is already running. Use `/ stop auto` to stop it.")
        return
    state.start_auto()
    t = threading.Thread(target=run_auto_pipeline, args=(say,), daemon=True)
    state.auto_thread = t
    t.start()


def _handle_stop_auto(say):
    if not state.is_auto_running():
        say("Auto mode is not running.")
        return
    state.stop_auto()
    say("Stopping auto mode... (will stop after current operation completes)")


def _handle_status_auto(say):
    running = state.is_auto_running()
    sent_count = email_sender.get_sent_count()
    say(f"*Auto Status*\n"
        f"Auto mode: {'RUNNING' if running else 'STOPPED'}\n"
        f"Total emails sent: {sent_count}")


# track pipeline
def _handle_track(say):
    if state.is_tracking():
        say("Reply tracking is already running. Use `/ stop track` to stop it.")
        return
    state.start_tracking()
    t = threading.Thread(target=run_track_pipeline, args=(say,), daemon=True)
    state.tracking_thread = t
    t.start()


def _handle_stop_track(say):
    if not state.is_tracking():
        say("Reply tracking is not running.")
        return
    state.stop_tracking()
    say("Stopping reply tracking...")


def _handle_status_track(say):
    tracking = state.is_tracking()
    reply_log = reply_tracker.get_reply_log()
    human = sum(1 for r in reply_log if r["reply_type"] == "human")
    bounces = sum(1 for r in reply_log if r["reply_type"] == "bounce")
    ooo = sum(1 for r in reply_log if r["reply_type"] == "ooo")
    spam = sum(1 for r in reply_log if r["reply_type"] == "spam_auto")
    auto = sum(1 for r in reply_log if r["reply_type"] == "auto_reply")

    # 统计情感分布
    analyzed = [r for r in reply_log if r["reply_type"] == "human" and r.get("analysis")]
    interested = sum(1 for r in analyzed if r["analysis"].get("sentiment") == "interested")
    rejected = sum(1 for r in analyzed if r["analysis"].get("sentiment") == "rejected")
    neutral = sum(1 for r in analyzed if r["analysis"].get("sentiment") == "neutral")

    say(f"*Track Status*\n"
        f"Tracking: {'RUNNING' if tracking else 'STOPPED'}\n"
        f"First replies: {human} human | {bounces} bounces | {ooo} OOO | {spam} spam | {auto} auto-reply\n"
        f"Sentiment: {interested} interested | {rejected} rejected | {neutral} neutral")


# ── Usage ───────────────────────────────────────────────────────────

def _handle_usage(text: str, say):
    rest = re.sub(r"^/\s*usage\s*", "", text, flags=re.IGNORECASE).strip()

    # / usage campaign_xxx → per-campaign detail
    if rest:
        report = usage_tracker.format_slack_report(rest)
        say(report)
        return

    # / usage → full overview with by-category breakdown + averages
    sent_count = email_sender.get_sent_count()
    reply_log = reply_tracker.get_reply_log()
    reply_count = sum(1 for r in reply_log if r["reply_type"] == "human")
    report = usage_tracker.format_full_slack_report(sent_count, reply_count)
    say(f"{report}\n\nTotal emails sent: {sent_count} | Human replies: {reply_count}")


# ── Prospect ──────────────────────────────────────────────────────────

def _handle_prospect(text: str, say):
    """/ prospect dental clinic in Los Angeles, auto repair in Houston --depth 5

    直接运行：爬取 Google Maps → 找邮箱 → 生成 CSV → 上传 Drive。
    不会发邮件，只生成线索。用逗号分隔多个搜索词。
    """
    rest = re.sub(r"^/\s*prospect\s+", "", text, flags=re.IGNORECASE).strip()
    if not rest:
        _handle_prospect_help(say)
        return

    # Parse --depth N
    depth = None
    depth_match = re.search(r"--depth\s+(\d+)", rest)
    if depth_match:
        depth = int(depth_match.group(1))
        rest = rest[:depth_match.start()].strip().rstrip(",").strip()

    queries = [q.strip() for q in rest.split(",") if q.strip()]
    if not queries:
        _handle_prospect_help(say)
        return
    t = threading.Thread(target=run_prospect, args=(queries, say, depth), daemon=True)
    t.start()


def _handle_prospect_help(say):
    say("*Prospect — Google Maps Lead Finder*\n"
        "Scrape Google Maps for businesses, find emails, upload CSV to Drive.\n"
        "*Does NOT send emails* — only generates leads.\n"
        "Auto-dedup: skips companies already prospected or contacted.\n\n"
        "*Usage:*\n"
        "`/ prospect dental clinic in Los Angeles`\n"
        "`/ prospect dental clinic in LA, auto repair in Houston`\n"
        "`/ prospect dental clinic in LA --depth 5`\n\n"
        "*Options:*\n"
        "`--depth N` — scraper scroll depth (default from .env)\n\n"
        "*Tip:* Use different area names to cover more ground:\n"
        "`/ prospect dental clinic in Santa Monica`\n"
        "`/ prospect dental clinic in Pasadena`\n\n"
        "After CSV appears in Drive, use `/ auto` to start sending.")
