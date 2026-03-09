"""Stats Engine — aggregates sent/reply data for reporting and insights.

Only tracks first replies, no multi-round conversations.
"""

import logging
from collections import defaultdict

from services.email_sender import get_sent_log
from services.reply_tracker import get_reply_log

log = logging.getLogger(__name__)


# Safe division, returns percentage. Returns 0 if divisor is 0.
def _safe_div(a, b):
    return round(a / b * 100, 1) if b else 0.0


# ── Overview ────────────────────────────────────────────────────────

# Overall campaign stats: sent count, reply rate, sentiment breakdown.
def get_overview() -> dict:
    sent = get_sent_log()
    replies = get_reply_log()

    total_sent = sum(1 for s in sent if s["status"] == "sent")
    human_replies = [r for r in replies if r["reply_type"] == "human"]
    bounces = [r for r in replies if r["reply_type"] == "bounce"]
    ooo = [r for r in replies if r["reply_type"] == "ooo"]
    spam = [r for r in replies if r["reply_type"] == "spam_auto"]

    # Sentiment breakdown from analyzed human replies
    analyzed = [r for r in human_replies if r.get("analysis")]
    interested = sum(1 for r in analyzed if r["analysis"].get("sentiment") == "interested")
    rejected = sum(1 for r in analyzed if r["analysis"].get("sentiment") == "rejected")
    neutral = sum(1 for r in analyzed if r["analysis"].get("sentiment") == "neutral")

    return {
        "total_sent": total_sent,
        "total_replies": len(human_replies),
        "reply_rate": _safe_div(len(human_replies), total_sent),
        "bounces": len(bounces),
        "bounce_rate": _safe_div(len(bounces), total_sent),
        "ooo": len(ooo),
        "spam_auto": len(spam),
        "sentiment": {"interested": interested, "rejected": rejected, "neutral": neutral},
        "avg_time_to_reply_hours": _avg_time_to_reply(human_replies),
    }


# Average time to reply in hours.
def _avg_time_to_reply(replies: list[dict]) -> float:
    times = [r["time_to_reply_hours"] for r in replies if r.get("time_to_reply_hours", 0) > 0]
    return round(sum(times) / len(times), 1) if times else 0.0


# ── Timing ──────────────────────────────────────────────────────────

# Reply timing distribution: how long it takes for prospects to reply.
def get_timing_stats() -> dict:
    replies = [r for r in get_reply_log() if r["reply_type"] == "human"]

    if not replies:
        return {"distribution": {}, "avg_hours": 0, "median_hours": 0}

    buckets = {"<1h": 0, "1-4h": 0, "4-12h": 0, "12-24h": 0, "1-3d": 0, "3-7d": 0, ">7d": 0}

    times = []
    for r in replies:
        h = r.get("time_to_reply_hours", 0)
        if h <= 0:
            continue
        times.append(h)
        if h < 1:
            buckets["<1h"] += 1
        elif h < 4:
            buckets["1-4h"] += 1
        elif h < 12:
            buckets["4-12h"] += 1
        elif h < 24:
            buckets["12-24h"] += 1
        elif h < 72:
            buckets["1-3d"] += 1
        elif h < 168:
            buckets["3-7d"] += 1
        else:
            buckets[">7d"] += 1

    times.sort()
    median = times[len(times) // 2] if times else 0

    return {
        "distribution": buckets,
        "avg_hours": round(sum(times) / len(times), 1) if times else 0,
        "median_hours": round(median, 1),
        "total_replies": len(times),
    }


# ── Industry Breakdown ──────────────────────────────────────────────

# Reply rate, sentiment, and company profile factors by industry.
def get_industry_stats() -> list[dict]:
    sent = get_sent_log()
    replies = get_reply_log()

    # Count sent emails per industry
    sent_by_industry = defaultdict(int)
    for s in sent:
        if s["status"] == "sent" and s.get("industry"):
            sent_by_industry[s["industry"]] += 1

    # Collect human replies per industry
    replies_by_industry = defaultdict(list)
    for r in replies:
        if r["reply_type"] == "human" and r.get("industry"):
            replies_by_industry[r["industry"]].append(r)

    results = []
    for industry in sorted(set(list(sent_by_industry.keys()) + list(replies_by_industry.keys()))):
        sent_count = sent_by_industry.get(industry, 0)
        ind_replies = replies_by_industry.get(industry, [])
        analyzed = [r for r in ind_replies if r.get("analysis")]

        # Sentiment counts for this industry
        sentiments = defaultdict(int)
        for r in analyzed:
            sentiments[r["analysis"].get("sentiment", "neutral")] += 1

        # Company profile factors (pain points, current solutions, size signals)
        pain_points = []
        current_solutions = []
        size_signals = defaultdict(int)
        for r in analyzed:
            factors = r["analysis"].get("company_factors", {})
            if factors.get("pain_point"):
                pain_points.append(factors["pain_point"])
            if factors.get("current_solution"):
                current_solutions.append(factors["current_solution"])
            if factors.get("size_signal"):
                size_signals[factors["size_signal"]] += 1

        results.append({
            "industry": industry,
            "sent": sent_count,
            "replies": len(ind_replies),
            "reply_rate": _safe_div(len(ind_replies), sent_count),
            "sentiment": dict(sentiments),
            "avg_time_to_reply_hours": _avg_time_to_reply(ind_replies),
            "pain_points": pain_points,
            "current_solutions": current_solutions,
            "size_distribution": dict(size_signals),
        })

    return sorted(results, key=lambda x: x["reply_rate"], reverse=True)


# ── Subject Line Stats ──────────────────────────────────────────────

# Per-email subject line performance (used by Slack report and insights summary).
def get_subject_stats() -> list[dict]:
    sent = get_sent_log()
    replies = get_reply_log()

    # thread_id -> first human reply
    reply_by_thread = {}
    for r in replies:
        if r["reply_type"] == "human":
            if r["thread_id"] not in reply_by_thread:
                reply_by_thread[r["thread_id"]] = r

    results = []
    for s in sent:
        if s["status"] != "sent" or not s.get("thread_id"):
            continue
        reply = reply_by_thread.get(s["thread_id"])
        got_reply = reply is not None
        sentiment = None
        if reply and reply.get("analysis"):
            sentiment = reply["analysis"].get("sentiment")

        results.append({
            "subject": s["subject"],
            "company": s["company_name"],
            "industry": s.get("industry", ""),
            "got_reply": got_reply,
            "sentiment": sentiment,
            "time_to_reply_hours": reply.get("time_to_reply_hours") if reply else None,
        })

    return results


# ── Email Length Stats ──────────────────────────────────────────────

# Reply rate by email body length bucket.
def get_length_stats() -> dict:
    sent = get_sent_log()
    replies = get_reply_log()

    reply_threads = {r["thread_id"] for r in replies if r["reply_type"] == "human"}

    buckets = {"short (<80)": [0, 0], "medium (80-120)": [0, 0], "long (>120)": [0, 0]}

    for s in sent:
        if s["status"] != "sent":
            continue
        length = s.get("body_length", 0)
        # Estimate word count (~5 chars per word)
        words = length // 5 if length else 0

        if words < 80:
            bucket = "short (<80)"
        elif words <= 120:
            bucket = "medium (80-120)"
        else:
            bucket = "long (>120)"

        buckets[bucket][0] += 1  # total sent
        if s.get("thread_id") in reply_threads:
            buckets[bucket][1] += 1  # got reply

    result = {}
    for bucket, (total, replied) in buckets.items():
        result[bucket] = {
            "sent": total,
            "replies": replied,
            "reply_rate": _safe_div(replied, total),
        }
    return result


# ── Reply Detail List ───────────────────────────────────────────────

# All human first replies, optionally filtered by sentiment.
def get_all_replies(sentiment_filter: str = None) -> list[dict]:
    replies = [r for r in get_reply_log() if r["reply_type"] == "human"]
    if sentiment_filter:
        replies = [r for r in replies if r.get("analysis", {}).get("sentiment") == sentiment_filter]
    return replies


# ── Compact Stats for Insights Agent ────────────────────────────────
# Principle: pre-aggregate in code, only send summaries to GPT.
# This reduces token usage from ~5000 to ~800 for 200 sent emails.

# Subject line summary: only include replied subjects + counts, skip the 192 no-reply entries.
def _get_subject_summary() -> dict:
    all_subjects = get_subject_stats()
    replied = [s for s in all_subjects if s["got_reply"]]
    no_reply_count = len(all_subjects) - len(replied)

    replied_subjects = []
    for s in replied:
        replied_subjects.append({
            "subject": s["subject"],
            "company": s["company"],
            "industry": s["industry"],
            "sentiment": s["sentiment"],
            "time_to_reply_hours": s["time_to_reply_hours"],
        })

    return {
        "total_sent": len(all_subjects),
        "got_reply": len(replied),
        "no_reply": no_reply_count,
        "replied_subjects": replied_subjects,
    }


# Reply analysis summary: extract core GPT analysis fields, exclude raw body text.
def _get_reply_analysis_summary() -> list[dict]:
    replies = get_all_replies()
    summaries = []
    for r in replies:
        a = r.get("analysis", {}) or {}
        factors = a.get("company_factors", {}) or {}
        summaries.append({
            "company": r.get("company_name", "?"),
            "industry": r.get("industry", "?"),
            "sentiment": a.get("sentiment", "?"),
            "intent": a.get("intent", "?"),
            "reason_summary": a.get("reason_summary", ""),
            "why": a.get("why_accepted_or_rejected", ""),
            "improvement_tip": a.get("improvement_tip", ""),
            "objections": a.get("objections", []),
            "size_signal": factors.get("size_signal"),
            "pain_point": factors.get("pain_point"),
            "current_solution": factors.get("current_solution"),
            "time_to_reply_hours": r.get("time_to_reply_hours", 0),
        })
    return summaries


# Build compact stats dict for the reporter agent (GPT insights).
def get_full_stats_for_insights() -> dict:
    return {
        "overview": get_overview(),
        "industry_breakdown": get_industry_stats(),
        "subject_summary": _get_subject_summary(),
        "length_analysis": get_length_stats(),
        "timing": get_timing_stats(),
        "reply_analyses": _get_reply_analysis_summary(),
    }


# ── Slack Report Formatting ─────────────────────────────────────────

# Full Slack report, code-only stats (no GPT call).
def format_full_report_slack() -> str:
    sections = []

    # ── 1. Overview ──────────────────────────────────────────────────
    o = get_overview()
    sections.append(
        f"*1. Overview*\n"
        f"Sent: {o['total_sent']}\n"
        f"Human replies: {o['total_replies']} ({o['reply_rate']}%)\n"
        f"Bounces: {o['bounces']} ({o['bounce_rate']}%)\n"
        f"OOO: {o['ooo']} | Spam notifications: {o['spam_auto']}\n"
        f"Avg time to reply: {o['avg_time_to_reply_hours']}h\n"
        f"Sentiment: {o['sentiment']['interested']} interested / "
        f"{o['sentiment']['neutral']} neutral / "
        f"{o['sentiment']['rejected']} rejected"
    )

    # ── 2. Industry Breakdown ────────────────────────────────────────
    ind_stats = get_industry_stats()
    if ind_stats:
        lines = ["*2. Reply Rate by Industry*"]
        for s in ind_stats:
            sentiment_str = ", ".join(f"{k}: {v}" for k, v in s.get("sentiment", {}).items())
            line = (
                f"  *{s['industry']}*: {s['replies']}/{s['sent']} "
                f"({s['reply_rate']}%) | {sentiment_str or 'n/a'}"
            )
            if s.get("pain_points"):
                line += f"\n    Pain points: {', '.join(s['pain_points'][:3])}"
            if s.get("current_solutions"):
                line += f"\n    Current solutions: {', '.join(s['current_solutions'][:3])}"
            lines.append(line)
        sections.append("\n".join(lines))

    # ── 3. Reply Timing ──────────────────────────────────────────────
    t = get_timing_stats()
    if t.get("total_replies"):
        lines = [
            f"*3. Reply Timing*",
            f"Avg: {t['avg_hours']}h | Median: {t['median_hours']}h",
        ]
        for bucket, count in t["distribution"].items():
            bar = "=" * count
            lines.append(f"  {bucket:>6}: {bar} {count}")
        sections.append("\n".join(lines))

    # ── 4. Subject Line Performance ──────────────────────────────────
    subjects = get_subject_stats()
    if subjects:
        replied = [s for s in subjects if s["got_reply"]]
        no_reply = [s for s in subjects if not s["got_reply"]]
        lines = [f"*4. Subject Lines* ({len(replied)}/{len(subjects)} got replies)"]
        for s in replied:
            lines.append(
                f'  "{s["subject"]}" -> {s["company"]} ({s["industry"]}) '
                f'| {s.get("sentiment", "?")} | {s.get("time_to_reply_hours", "?")}h'
            )
        if no_reply:
            lines.append(f"  No reply: {len(no_reply)} emails")
        sections.append("\n".join(lines))

    # ── 5. Email Length vs Reply Rate ────────────────────────────────
    length = get_length_stats()
    if any(v["sent"] > 0 for v in length.values()):
        lines = ["*5. Email Length vs Reply Rate*"]
        for bucket, v in length.items():
            if v["sent"] > 0:
                lines.append(f"  {bucket}: {v['replies']}/{v['sent']} ({v['reply_rate']}%)")
        sections.append("\n".join(lines))

    # ── 6. All Reply Details ─────────────────────────────────────────
    replies = get_all_replies()
    if replies:
        lines = [f"*6. All First Replies* ({len(replies)} total)"]
        for r in replies:
            a = r.get("analysis", {}) or {}
            factors = a.get("company_factors", {}) or {}
            line = (
                f"  *{r.get('company_name', '?')}* ({r.get('industry', '?')})\n"
                f"    From: {r.get('reply_from', '?')} | "
                f"{a.get('sentiment', '?')} | {a.get('intent', '?')}\n"
                f"    {r.get('time_to_reply_hours', '?')}h | {a.get('summary', '')}"
            )
            if a.get("why_accepted_or_rejected"):
                line += f"\n    Reason: {a['why_accepted_or_rejected']}"
            if a.get("follow_up_advice"):
                line += f"\n    Follow-up advice: {a['follow_up_advice']}"
            factor_parts = []
            if factors.get("size_signal") and factors["size_signal"] != "unknown":
                factor_parts.append(f"Size: {factors['size_signal']}")
            if factors.get("current_solution"):
                factor_parts.append(f"Current: {factors['current_solution']}")
            if factor_parts:
                line += f"\n    Profile: {' | '.join(factor_parts)}"
            lines.append(line)
        sections.append("\n".join(lines))

    if not sections:
        return "No data yet. Send some emails first, then use `/ track` to start reply tracking."

    return "\n\n".join(sections)
