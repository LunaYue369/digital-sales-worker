"""Report Pipeline — one-shot report and GPT insights (not a background loop).

Two entry points:
- run_report(say)   → code-only stats report (/ report)
- run_insights(say) → GPT deep insights + save to local log (/ insights)
"""

import json
import logging
import os
import time

from services import stats
from agents import reporter

log = logging.getLogger(__name__)

INSIGHTS_LOG_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "insights_log.json")


def run_report(say):
    """Code-only stats report, no GPT calls. Aggregates sent_log + reply_log data."""
    say(stats.format_full_report_slack())


def run_insights(say):
    """GPT deep insights: collect full stats → reporter agent analysis → save to local log → Slack notification."""
    say("Generating insights... (this may take a moment)")

    try:
        # Collect full stats
        full_stats = stats.get_full_stats_for_insights()

        # Check if there's enough data
        if full_stats["overview"]["total_sent"] == 0:
            say("No emails sent yet — nothing to analyze.")
            return

        # Call reporter agent (GPT) to generate insights
        result = reporter.generate_insights(full_stats)

        # Save to local insights log
        _save_insights(result)

        # Format Slack message
        msg = _format_insights_slack(result)
        say(msg)
        say("_Insights saved to `data/insights_log.json`. "
            "Review copywriter feedback above and update `agents/souls/copywriter.md` as needed._")

    except Exception as e:
        log.error("Insights generation failed: %s", e, exc_info=True)
        say(f"Failed to generate insights: {e}")


def _format_insights_slack(result: dict) -> str:
    """Format reporter agent insights dict into a Slack message."""
    lines = []

    if result.get("overall_summary"):
        lines.append(f"*Campaign Insights*\n{result['overall_summary']}\n")

    if result.get("top_performing_patterns"):
        lines.append("*Top Performing Patterns:*")
        for p in result["top_performing_patterns"]:
            lines.append(f"  - {p.get('pattern', '')} ({p.get('evidence', '')})")
        lines.append("")

    if result.get("industry_insights"):
        lines.append("*Industry Insights:*")
        for ind in result["industry_insights"]:
            lines.append(
                f"  *{ind.get('industry', '?')}* ({ind.get('reply_rate', '?')}): "
                f"{ind.get('recommendation', '')}"
            )
        lines.append("")

    if result.get("subject_line_insights"):
        s = result["subject_line_insights"]
        lines.append(f"*Subject Lines:* {s.get('pattern', s.get('recommendation', ''))}")
        lines.append("")

    if result.get("length_insights"):
        l = result["length_insights"]
        lines.append(f"*Email Length:* {l.get('finding', l.get('recommendation', ''))}")
        lines.append("")

    if result.get("timing_insights"):
        t = result["timing_insights"]
        lines.append(f"*Timing:* {t.get('finding', t.get('recommendation', ''))}")
        lines.append("")

    if result.get("copywriter_feedback"):
        feedback = result["copywriter_feedback"]
        lines.append("*Copywriter Feedback (use this to update copywriter.md):*")
        if isinstance(feedback, list):
            for item in feedback:
                lines.append(f"  - {item}")
        else:
            lines.append(f"  {feedback}")
        lines.append("")

    if result.get("warnings"):
        lines.append("*Warnings:*")
        for w in result["warnings"]:
            lines.append(f"  - {w}")

    return "\n".join(lines) if lines else "Could not generate insights."


def _save_insights(result: dict):
    """Append insights to local log file data/insights_log.json."""
    existing = []
    if os.path.exists(INSIGHTS_LOG_PATH):
        with open(INSIGHTS_LOG_PATH, "r", encoding="utf-8") as f:
            existing = json.load(f)
    existing.append({
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "insights": result,
    })
    os.makedirs(os.path.dirname(INSIGHTS_LOG_PATH), exist_ok=True)
    with open(INSIGHTS_LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)
