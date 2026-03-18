"""Token usage tracking — per-user, thread-safe, with cumulative totals."""

import json
import logging
import os
import threading
import time

from core.user_config import user_data_dir

log = logging.getLogger(__name__)

_lock = threading.Lock()

# GPT-4.1-mini pricing (per 1K tokens): $0.40/M input, $1.60/M output
_COST_PER_1K = {"prompt": 0.0004, "completion": 0.0016}

_SEND_STEPS = {"researcher", "copywriter", "reviewer"}
_REPLY_STEPS = {"reply_analyzer"}
_REPORT_STEPS = {"reporter"}


def _data_path(user_id: str) -> str:
    return os.path.join(user_data_dir(user_id), "usage_log.json")


def _empty_totals() -> dict:
    return {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "estimated_cost": 0.0,
        "api_calls": 0,
    }


def _load(user_id: str) -> dict:
    path = _data_path(user_id)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            totals = _empty_totals()
            for e in data:
                totals["prompt_tokens"] += e.get("prompt_tokens", 0)
                totals["completion_tokens"] += e.get("completion_tokens", 0)
                totals["total_tokens"] += e.get("prompt_tokens", 0) + e.get("completion_tokens", 0)
                totals["estimated_cost"] += e.get("estimated_cost", 0)
                totals["api_calls"] += 1
            totals["estimated_cost"] = round(totals["estimated_cost"], 6)
            return {"records": data, "totals": totals}
        return data
    return {"records": [], "totals": _empty_totals()}


def _save(user_id: str, data: dict):
    path = _data_path(user_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _estimate_cost(prompt_tokens: int, completion_tokens: int) -> float:
    return round(
        prompt_tokens / 1000 * _COST_PER_1K["prompt"]
        + completion_tokens / 1000 * _COST_PER_1K["completion"],
        6,
    )


def record(user_id: str, campaign_id: str, step: str, prompt_tokens: int, completion_tokens: int):
    """Record a single GPT API call. Thread-safe."""
    cost = _estimate_cost(prompt_tokens, completion_tokens)
    entry = {
        "campaign_id": campaign_id,
        "step": step,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "estimated_cost": cost,
        "timestamp": time.time(),
    }

    with _lock:
        data = _load(user_id)
        data["records"].append(entry)
        t = data["totals"]
        t["prompt_tokens"] += prompt_tokens
        t["completion_tokens"] += completion_tokens
        t["total_tokens"] += prompt_tokens + completion_tokens
        t["estimated_cost"] = round(t["estimated_cost"] + cost, 6)
        t["api_calls"] += 1
        _save(user_id, data)

    return entry


def get_campaign_summary(user_id: str, campaign_id: str) -> dict:
    with _lock:
        data = _load(user_id)

    entries = [e for e in data["records"] if e["campaign_id"] == campaign_id]
    if not entries:
        return {"campaign_id": campaign_id, "total_calls": 0}

    by_step: dict[str, dict] = {}
    total_prompt = total_completion = 0
    total_cost = 0.0

    for e in entries:
        step = e["step"]
        if step not in by_step:
            by_step[step] = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0}
        by_step[step]["calls"] += 1
        by_step[step]["prompt_tokens"] += e["prompt_tokens"]
        by_step[step]["completion_tokens"] += e["completion_tokens"]
        by_step[step]["cost"] += e["estimated_cost"]
        total_prompt += e["prompt_tokens"]
        total_completion += e["completion_tokens"]
        total_cost += e["estimated_cost"]

    return {
        "campaign_id": campaign_id,
        "total_calls": len(entries),
        "total_prompt_tokens": total_prompt,
        "total_completion_tokens": total_completion,
        "total_tokens": total_prompt + total_completion,
        "total_cost": round(total_cost, 4),
        "by_step": by_step,
    }


def get_all_summary(user_id: str) -> dict:
    with _lock:
        data = _load(user_id)
    t = data["totals"]
    records = data["records"]
    campaigns = set(e["campaign_id"] for e in records if e["campaign_id"] not in ("reporter", "tracking"))

    categories = {
        "send": {"calls": 0, "tokens": 0, "cost": 0.0},
        "reply_analyzer": {"calls": 0, "tokens": 0, "cost": 0.0},
        "reporter": {"calls": 0, "tokens": 0, "cost": 0.0},
    }
    for e in records:
        step = e["step"]
        tokens = e.get("prompt_tokens", 0) + e.get("completion_tokens", 0)
        cost = e.get("estimated_cost", 0)
        if step in _SEND_STEPS:
            cat = "send"
        elif step in _REPLY_STEPS:
            cat = "reply_analyzer"
        elif step in _REPORT_STEPS:
            cat = "reporter"
        else:
            cat = "send"
        categories[cat]["calls"] += 1
        categories[cat]["tokens"] += tokens
        categories[cat]["cost"] += cost

    return {
        "total_campaigns": len(campaigns),
        "total_calls": t["api_calls"],
        "total_tokens": t["total_tokens"],
        "total_cost": round(t["estimated_cost"], 4),
        "categories": categories,
    }


def format_slack_report(user_id: str, campaign_id: str) -> str:
    s = get_campaign_summary(user_id, campaign_id)
    if s["total_calls"] == 0:
        return f"\u2139\ufe0f Campaign `{campaign_id}`: no GPT calls recorded."

    sep = "\u2500" * 30
    lines = [
        f"\U0001f4cb *Campaign Usage* \u2014 `{campaign_id}`",
        sep,
        f"\U0001f916 API calls: *{s['total_calls']}*",
        f"\U0001f4ac Tokens: *{s['total_tokens']:,}*  (in: {s['total_prompt_tokens']:,} / out: {s['total_completion_tokens']:,})",
        f"\U0001f4b0 Cost: *${s['total_cost']:.4f}*",
        f"\n\U0001f527 *By Step*\n{sep}",
    ]
    for step, info in s["by_step"].items():
        tokens = info['prompt_tokens'] + info['completion_tokens']
        lines.append(
            f"  \u25b6\ufe0f `{step}` \u2014 {info['calls']} calls \u2022 "
            f"{tokens:,} tokens \u2022 ${info['cost']:.4f}"
        )
    return "\n".join(lines)


def _format_category_block(label: str, emoji: str, c: dict, unit_label: str = "", unit_count: int = 0) -> list[str]:
    lines = [
        f"  {emoji} *{label}*",
        f"      {c['calls']} calls \u2022 {c['tokens']:,} tokens \u2022 ${c['cost']:.4f}",
    ]
    if unit_count > 0:
        avg_tokens = c['tokens'] // unit_count
        avg_cost = c['cost'] / unit_count
        lines.append(f"      Avg per {unit_label}: {avg_tokens:,} tokens \u2022 ${avg_cost:.4f}")
    return lines


def format_full_slack_report(user_id: str, sent_count: int, reply_count: int) -> str:
    s = get_all_summary(user_id)
    cats = s["categories"]
    sep = "\u2500" * 30

    lines = [
        f"\U0001f4b3 *Token Usage*",
        sep,
        f"\U0001f4e6 Campaigns: *{s['total_campaigns']}*",
        f"\U0001f916 API calls: *{s['total_calls']}*",
        f"\U0001f4ac Tokens: *{s['total_tokens']:,}*",
        f"\U0001f4b0 Total cost: *${s['total_cost']:.4f}*",
        f"\U0001f4e4 Emails sent: *{sent_count}*  \u2022  \U0001f4e8 Replies: *{reply_count}*",
        f"\n\U0001f4ca *Breakdown*\n{sep}",
    ]

    lines += _format_category_block(
        "Sending", "\U0001f4e4", cats["send"], "email", sent_count
    )
    lines.append("")
    lines += _format_category_block(
        "Reply Analysis", "\U0001f50d", cats["reply_analyzer"], "reply", reply_count
    )
    lines.append("")
    lines += _format_category_block(
        "Insights", "\U0001f9e0", cats["reporter"], "report", cats["reporter"]["calls"]
    )

    return "\n".join(lines)


def _safe_div(a, b):
    return round(a / b * 100, 1) if b else 0.0


def format_all_users_slack_report() -> str:
    """Admin dashboard — per-person business KPIs + token costs."""
    from core.user_config import list_users
    from services import email_sender, reply_tracker
    from services.stats import get_overview, get_industry_stats

    users = list_users()
    if not users:
        return "\u2139\ufe0f No registered users."

    sep = "\u2500" * 30
    lines = []

    # ── Collect per-user data ──
    grand_sent = grand_replies = grand_interested = 0
    grand_rejected = grand_neutral = 0
    grand_bounces = 0
    grand_tokens = 0
    grand_cost = 0.0
    user_rows = []

    for uid, cfg in users.items():
        if cfg.get("role") == "admin":
            continue
        name = cfg.get("name", uid)
        overview = get_overview(uid)
        usage = get_all_summary(uid)
        industries = get_industry_stats(uid)

        grand_sent += overview["total_sent"]
        grand_replies += overview["total_replies"]
        grand_interested += overview["sentiment"]["interested"]
        grand_rejected += overview["sentiment"]["rejected"]
        grand_neutral += overview["sentiment"]["neutral"]
        grand_bounces += overview["bounces"]
        grand_tokens += usage["total_tokens"]
        grand_cost += usage["total_cost"]

        user_rows.append((name, overview, usage, industries))

    # ── Team Summary ──
    grand_reply_rate = _safe_div(grand_replies, grand_sent)

    lines.append(f"\U0001f451 *Team Dashboard*")
    lines.append(sep)
    lines.append(
        f"\U0001f4e4 Emails sent: *{grand_sent}*\n"
        f"\U0001f4e8 Replies: *{grand_replies}* ({grand_reply_rate}%)\n"
        f"\u26a0\ufe0f Bounces: *{grand_bounces}*\n"
        f"\U0001f7e2 Interested: *{grand_interested}*  \u2022  "
        f"\U0001f7e1 Neutral: *{grand_neutral}*  \u2022  "
        f"\U0001f534 Rejected: *{grand_rejected}*\n"
        f"\U0001f4ac Tokens: *{grand_tokens:,}*  \u2022  "
        f"\U0001f4b0 Cost: *${grand_cost:.4f}*"
    )

    # ── Per-User Detail ──
    for name, overview, usage, industries in user_rows:
        sent = overview["total_sent"]
        replies = overview["total_replies"]
        reply_rate = overview["reply_rate"]
        s = overview["sentiment"]
        cats = usage["categories"]

        lines.append(f"\n\U0001f464 *{name}*")
        lines.append(sep)

        # Business KPIs
        lines.append(
            f"  \U0001f4e4 Sent: *{sent}*  \u2022  "
            f"\U0001f4e8 Replies: *{replies}* ({reply_rate}%)"
        )
        lines.append(
            f"  \U0001f7e2 {s['interested']}  \u2022  "
            f"\U0001f7e1 {s['neutral']}  \u2022  "
            f"\U0001f534 {s['rejected']}  \u2022  "
            f"\u26a0\ufe0f {overview['bounces']} bounces"
        )
        if overview["avg_time_to_reply_hours"] > 0:
            lines.append(
                f"  \u23f1\ufe0f Avg reply time: *{overview['avg_time_to_reply_hours']}h*"
            )

        # Top industries
        if industries:
            top = industries[:3]
            ind_parts = []
            for ind in top:
                ind_parts.append(
                    f"{ind['industry']} {ind['replies']}/{ind['sent']} ({ind['reply_rate']}%)"
                )
            lines.append(f"  \U0001f3ed Top: {' | '.join(ind_parts)}")

        # Token costs
        lines.append(
            f"  \U0001f4b0 *{usage['total_tokens']:,}* tokens \u2022 "
            f"${usage['total_cost']:.4f}"
        )
        if sent > 0:
            lines.append(
                f"      Avg/email: ${usage['total_cost'] / sent:.4f}"
            )

        # Mini breakdown (only non-zero)
        breakdown_parts = []
        for label, key in [("Send", "send"), ("Analyze", "reply_analyzer"), ("Insights", "reporter")]:
            c = cats[key]
            if c["calls"] > 0:
                breakdown_parts.append(f"{label}: ${c['cost']:.4f}")
        if breakdown_parts:
            lines.append(f"      {' \u2022 '.join(breakdown_parts)}")

    return "\n".join(lines)
