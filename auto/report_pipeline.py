"""Report Pipeline — per-user one-shot report and GPT insights.

Two entry points:
- run_report(user_id, say)   → code-only stats report (/ report)
- run_insights(user_id, say) → GPT deep insights + save to local log (/ insights)
"""

import json
import logging
import os
import time

from services import stats
from agents import reporter
from core.user_config import user_data_dir

log = logging.getLogger(__name__)


def _insights_log_path(user_id: str) -> str:
    return os.path.join(user_data_dir(user_id), "insights_log.json")


def run_report(user_id: str, say):
    """Code-only stats report, no GPT calls."""
    say(stats.format_full_report_slack(user_id))


def run_insights(user_id: str, say):
    """GPT deep insights: collect full stats → reporter agent → save → Slack."""
    say("\U0001f9e0 *Generating insights...* (this may take a moment)")

    try:
        full_stats = stats.get_full_stats_for_insights(user_id)

        if full_stats["overview"]["total_sent"] == 0:
            say("No emails sent yet — nothing to analyze.")
            return

        result = reporter.generate_insights(full_stats, user_id)

        _save_insights(user_id, result)

        msg = _format_insights_slack(result)
        say(msg)
        say("\u2705 _Insights saved. Review copywriter feedback above and update souls as needed._")

    except Exception as e:
        log.error("Insights generation failed: %s", e, exc_info=True)
        say(f"Failed to generate insights: {e}")


def _format_insights_slack(result: dict) -> str:
    lines = []
    sep = "─" * 30

    if result.get("overall_summary"):
        lines.append(f"\U0001f9e0 *Campaign Insights*\n{sep}\n{result['overall_summary']}")

    if result.get("top_performing_patterns"):
        lines.append(f"\n\U0001f31f *Top Performing Patterns*\n{sep}")
        for p in result["top_performing_patterns"]:
            lines.append(f"  \u2022 {p.get('pattern', '')}  _({p.get('evidence', '')})_")

    if result.get("industry_insights"):
        lines.append(f"\n\U0001f3ed *Industry Insights*\n{sep}")
        for ind in result["industry_insights"]:
            lines.append(
                f"  \u25b6\ufe0f *{ind.get('industry', '?')}* ({ind.get('reply_rate', '?')})\n"
                f"      {ind.get('recommendation', '')}"
            )

    if result.get("subject_line_insights"):
        s = result["subject_line_insights"]
        lines.append(
            f"\n\u2709\ufe0f *Subject Lines*\n{sep}\n"
            f"  {s.get('pattern', s.get('recommendation', ''))}"
        )

    if result.get("length_insights"):
        li = result["length_insights"]
        lines.append(
            f"\n\U0001f4cf *Email Length*\n{sep}\n"
            f"  {li.get('finding', li.get('recommendation', ''))}"
        )

    if result.get("timing_insights"):
        t = result["timing_insights"]
        lines.append(
            f"\n\u23f0 *Timing*\n{sep}\n"
            f"  {t.get('finding', t.get('recommendation', ''))}"
        )

    if result.get("copywriter_feedback"):
        feedback = result["copywriter_feedback"]
        lines.append(f"\n\u270d\ufe0f *Copywriter Feedback*\n{sep}")
        if isinstance(feedback, list):
            for item in feedback:
                lines.append(f"  \u2022 {item}")
        else:
            lines.append(f"  {feedback}")

    if result.get("warnings"):
        lines.append(f"\n\u26a0\ufe0f *Warnings*\n{sep}")
        for w in result["warnings"]:
            lines.append(f"  \u2022 {w}")

    return "\n".join(lines) if lines else "\u2753 Could not generate insights."


def _save_insights(user_id: str, result: dict):
    path = _insights_log_path(user_id)
    existing = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            existing = json.load(f)
    existing.append({
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "insights": result,
    })
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)
