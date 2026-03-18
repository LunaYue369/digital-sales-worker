"""Stats Engine — per-user, aggregates sent/reply data for reporting and insights.

Only tracks first replies, no multi-round conversations.
"""

import logging
from collections import defaultdict

from services import email_sender, reply_tracker

log = logging.getLogger(__name__)


def _safe_div(a, b):
    return round(a / b * 100, 1) if b else 0.0


# ── Overview ────────────────────────────────────────────────────────

def get_overview(user_id: str) -> dict:
    sent = email_sender.get_sent_log(user_id)
    replies = reply_tracker.get_reply_log(user_id)

    total_sent = sum(1 for s in sent if s["status"] == "sent")
    human_replies = [r for r in replies if r["reply_type"] == "human"]
    bounces = [r for r in replies if r["reply_type"] == "bounce"]
    ooo = [r for r in replies if r["reply_type"] == "ooo"]
    spam = [r for r in replies if r["reply_type"] == "spam_auto"]

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


def _avg_time_to_reply(replies: list[dict]) -> float:
    times = [r["time_to_reply_hours"] for r in replies if r.get("time_to_reply_hours", 0) > 0]
    return round(sum(times) / len(times), 1) if times else 0.0


# ── Timing ──────────────────────────────────────────────────────────

def get_timing_stats(user_id: str) -> dict:
    replies = [r for r in reply_tracker.get_reply_log(user_id) if r["reply_type"] == "human"]

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

def get_industry_stats(user_id: str) -> list[dict]:
    sent = email_sender.get_sent_log(user_id)
    replies = reply_tracker.get_reply_log(user_id)

    sent_by_industry = defaultdict(int)
    for s in sent:
        if s["status"] == "sent" and s.get("industry"):
            sent_by_industry[s["industry"]] += 1

    replies_by_industry = defaultdict(list)
    for r in replies:
        if r["reply_type"] == "human" and r.get("industry"):
            replies_by_industry[r["industry"]].append(r)

    results = []
    for industry in sorted(set(list(sent_by_industry.keys()) + list(replies_by_industry.keys()))):
        sent_count = sent_by_industry.get(industry, 0)
        ind_replies = replies_by_industry.get(industry, [])
        analyzed = [r for r in ind_replies if r.get("analysis")]

        sentiments = defaultdict(int)
        for r in analyzed:
            sentiments[r["analysis"].get("sentiment", "neutral")] += 1

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

def get_subject_stats(user_id: str) -> list[dict]:
    sent = email_sender.get_sent_log(user_id)
    replies = reply_tracker.get_reply_log(user_id)

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

def get_length_stats(user_id: str) -> dict:
    sent = email_sender.get_sent_log(user_id)
    replies = reply_tracker.get_reply_log(user_id)

    reply_threads = {r["thread_id"] for r in replies if r["reply_type"] == "human"}

    buckets = {"short (<80)": [0, 0], "medium (80-120)": [0, 0], "long (>120)": [0, 0]}

    for s in sent:
        if s["status"] != "sent":
            continue
        length = s.get("body_length", 0)
        words = length // 5 if length else 0

        if words < 80:
            bucket = "short (<80)"
        elif words <= 120:
            bucket = "medium (80-120)"
        else:
            bucket = "long (>120)"

        buckets[bucket][0] += 1
        if s.get("thread_id") in reply_threads:
            buckets[bucket][1] += 1

    result = {}
    for bucket, (total, replied) in buckets.items():
        result[bucket] = {
            "sent": total,
            "replies": replied,
            "reply_rate": _safe_div(replied, total),
        }
    return result


# ── Reply Detail List ───────────────────────────────────────────────

def get_all_replies(user_id: str, sentiment_filter: str = None) -> list[dict]:
    replies = [r for r in reply_tracker.get_reply_log(user_id) if r["reply_type"] == "human"]
    if sentiment_filter:
        replies = [r for r in replies if r.get("analysis", {}).get("sentiment") == sentiment_filter]
    return replies


# ── Compact Stats for Insights Agent ────────────────────────────────

def _get_subject_summary(user_id: str) -> dict:
    all_subjects = get_subject_stats(user_id)
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


def _get_reply_analysis_summary(user_id: str) -> list[dict]:
    replies = get_all_replies(user_id)
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


def get_full_stats_for_insights(user_id: str) -> dict:
    return {
        "overview": get_overview(user_id),
        "industry_breakdown": get_industry_stats(user_id),
        "subject_summary": _get_subject_summary(user_id),
        "length_analysis": get_length_stats(user_id),
        "timing": get_timing_stats(user_id),
        "reply_analyses": _get_reply_analysis_summary(user_id),
    }


# ── Slack Report Formatting ─────────────────────────────────────────

_SENTIMENT_EMOJI = {
    "interested": "\U0001f7e2",
    "rejected": "\U0001f534",
    "neutral": "\U0001f7e1",
}


def format_full_report_slack(user_id: str) -> str:
    sections = []

    # ── 1. Overview ──
    o = get_overview(user_id)
    s = o["sentiment"]
    sections.append(
        f"\U0001f4ca *Campaign Overview*\n"
        f"{'─' * 30}\n"
        f"\U0001f4e4 Sent: *{o['total_sent']}*\n"
        f"\U0001f4e8 Human replies: *{o['total_replies']}* ({o['reply_rate']}%)\n"
        f"\u26a0\ufe0f Bounces: {o['bounces']} ({o['bounce_rate']}%)  \u2022  "
        f"\U0001f3d6\ufe0f OOO: {o['ooo']}  \u2022  "
        f"\U0001f6ab Spam: {o['spam_auto']}\n"
        f"\u23f1\ufe0f Avg reply time: *{o['avg_time_to_reply_hours']}h*\n\n"
        f"\U0001f7e2 Interested: *{s['interested']}*  \u2022  "
        f"\U0001f7e1 Neutral: *{s['neutral']}*  \u2022  "
        f"\U0001f534 Rejected: *{s['rejected']}*"
    )

    # ── 2. Industry Breakdown ──
    ind_stats = get_industry_stats(user_id)
    if ind_stats:
        lines = [f"\U0001f3ed *Industry Breakdown*\n{'─' * 30}"]
        for st in ind_stats:
            sentiment_parts = []
            for k, v in st.get("sentiment", {}).items():
                emoji = _SENTIMENT_EMOJI.get(k, "")
                sentiment_parts.append(f"{emoji} {v}")
            sentiment_str = "  ".join(sentiment_parts) if sentiment_parts else "—"

            lines.append(
                f"\n\u25b6\ufe0f *{st['industry']}* — "
                f"{st['replies']}/{st['sent']} replies ({st['reply_rate']}%)"
            )
            lines.append(f"    {sentiment_str}")
            if st.get("pain_points"):
                lines.append(f"    \U0001f4a2 Pain points: {', '.join(st['pain_points'][:3])}")
            if st.get("current_solutions"):
                lines.append(f"    \U0001f527 Current solutions: {', '.join(st['current_solutions'][:3])}")
        sections.append("\n".join(lines))

    # ── 3. Reply Timing ──
    t = get_timing_stats(user_id)
    if t.get("total_replies"):
        lines = [
            f"\u23f0 *Reply Timing*\n{'─' * 30}",
            f"Avg: *{t['avg_hours']}h*  \u2022  Median: *{t['median_hours']}h*\n",
        ]
        max_count = max(t["distribution"].values()) if t["distribution"] else 1
        for bucket, count in t["distribution"].items():
            if count == 0:
                continue
            bar_len = round(count / max(max_count, 1) * 8)
            bar = "\u2588" * max(bar_len, 1)
            lines.append(f"  `{bucket:>6}` {bar} {count}")
        sections.append("\n".join(lines))

    # ── 4. Subject Lines ──
    subjects = get_subject_stats(user_id)
    if subjects:
        replied = [s for s in subjects if s["got_reply"]]
        no_reply = [s for s in subjects if not s["got_reply"]]
        lines = [
            f"\u2709\ufe0f *Subject Lines* — {len(replied)}/{len(subjects)} got replies\n"
            f"{'─' * 30}"
        ]
        for s in replied:
            emoji = _SENTIMENT_EMOJI.get(s.get("sentiment"), "\u2753")
            time_str = f"{s['time_to_reply_hours']}h" if s.get("time_to_reply_hours") else "?"
            lines.append(
                f"  {emoji} \"{s['subject']}\"\n"
                f"      \u2192 {s['company']} ({s['industry']})  \u2022  {time_str}"
            )
        if no_reply:
            lines.append(f"\n  \u2b1c No reply: {len(no_reply)} emails")
        sections.append("\n".join(lines))

    # ── 5. Email Length ──
    length = get_length_stats(user_id)
    if any(v["sent"] > 0 for v in length.values()):
        lines = [f"\U0001f4cf *Email Length vs Reply Rate*\n{'─' * 30}"]
        for bucket, v in length.items():
            if v["sent"] > 0:
                pct = v["reply_rate"]
                bar_len = round(pct / 10)
                bar = "\u2588" * max(bar_len, 1) if pct > 0 else "\u2581"
                lines.append(f"  `{bucket:>15}` {bar} {v['replies']}/{v['sent']} ({pct}%)")
        sections.append("\n".join(lines))

    # ── 6. All Replies ──
    replies = get_all_replies(user_id)
    if replies:
        lines = [f"\U0001f4ec *All First Replies* — {len(replies)} total\n{'─' * 30}"]
        for r in replies:
            a = r.get("analysis", {}) or {}
            factors = a.get("company_factors", {}) or {}
            sentiment = a.get("sentiment", "?")
            emoji = _SENTIMENT_EMOJI.get(sentiment, "\u2753")

            lines.append(
                f"\n{emoji} *{r.get('company_name', '?')}* ({r.get('industry', '?')})"
            )
            lines.append(
                f"  \U0001f4e8 {r.get('reply_from', '?')}  \u2022  "
                f"\u23f1\ufe0f {r.get('time_to_reply_hours', '?')}h"
            )
            lines.append(
                f"  \U0001f3af {a.get('sentiment', '?')}  \u2022  "
                f"\U0001f4ac {a.get('intent', '?')}"
            )
            if a.get("reason_summary"):
                lines.append(f"  \U0001f4cb {a['reason_summary']}")
            if a.get("why_accepted_or_rejected"):
                lines.append(f"  \U0001f50d {a['why_accepted_or_rejected']}")
            if a.get("follow_up_advice"):
                lines.append(f"  \U0001f4a1 {a['follow_up_advice']}")

            factor_parts = []
            if factors.get("size_signal") and factors["size_signal"] != "unknown":
                factor_parts.append(f"Size: {factors['size_signal']}")
            if factors.get("pain_point"):
                factor_parts.append(f"Pain: {factors['pain_point']}")
            if factors.get("current_solution"):
                factor_parts.append(f"Current: {factors['current_solution']}")
            if factor_parts:
                lines.append(f"  \U0001f3e2 {' \u2022 '.join(factor_parts)}")
        sections.append("\n".join(lines))

    if not sections:
        return "\u2139\ufe0f No data yet. Send some emails first, then use `/ track` to start reply tracking."

    return "\n\n".join(sections)
