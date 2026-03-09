"""Token usage tracking — thread-safe, with cumulative totals."""

import json
import logging
import os
import threading
import time

log = logging.getLogger(__name__)

DATA_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "usage_log.json")
_lock = threading.Lock()

# GPT-4.1-mini pricing (per 1K tokens): $0.40/M input, $1.60/M output
_COST_PER_1K = {"prompt": 0.0004, "completion": 0.0016}

# Steps that belong to the outbound email pipeline
_SEND_STEPS = {"researcher", "copywriter", "reviewer"}
# Steps that belong to reply analysis
_REPLY_STEPS = {"reply_analyzer"}
# Steps that belong to reporting
_REPORT_STEPS = {"reporter"}


def _empty_totals() -> dict:
    return {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "estimated_cost": 0.0,
        "api_calls": 0,
    }


def _load() -> dict:
    if os.path.exists(DATA_PATH):
        with open(DATA_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Migrate from old list format
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


def _save(data: dict):
    os.makedirs(os.path.dirname(DATA_PATH), exist_ok=True)
    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _estimate_cost(prompt_tokens: int, completion_tokens: int) -> float:
    return round(
        prompt_tokens / 1000 * _COST_PER_1K["prompt"]
        + completion_tokens / 1000 * _COST_PER_1K["completion"],
        6,
    )


def record(campaign_id: str, step: str, prompt_tokens: int, completion_tokens: int):
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
        data = _load()
        data["records"].append(entry)
        t = data["totals"]
        t["prompt_tokens"] += prompt_tokens
        t["completion_tokens"] += completion_tokens
        t["total_tokens"] += prompt_tokens + completion_tokens
        t["estimated_cost"] = round(t["estimated_cost"] + cost, 6)
        t["api_calls"] += 1
        _save(data)

    return entry


def get_campaign_summary(campaign_id: str) -> dict:
    """Summarize token usage for a specific campaign."""
    with _lock:
        data = _load()

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


def get_all_summary() -> dict:
    """Get cumulative totals with by-category breakdown."""
    with _lock:
        data = _load()
    t = data["totals"]
    records = data["records"]
    campaigns = set(e["campaign_id"] for e in records if e["campaign_id"] not in ("reporter", "tracking"))

    # Aggregate by category: send / reply_analyzer / reporter
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


def format_slack_report(campaign_id: str) -> str:
    """Format a Slack-friendly usage report for a specific campaign."""
    s = get_campaign_summary(campaign_id)
    if s["total_calls"] == 0:
        return f"Campaign `{campaign_id}`: no GPT calls recorded."

    lines = [
        f"*Campaign `{campaign_id}` — Token Usage Report*",
        f"Total GPT calls: {s['total_calls']}",
        f"Total tokens: {s['total_tokens']:,} (prompt: {s['total_prompt_tokens']:,} / completion: {s['total_completion_tokens']:,})",
        f"Estimated cost: ${s['total_cost']:.4f}",
        "",
        "*Breakdown by step:*",
    ]
    for step, info in s["by_step"].items():
        lines.append(
            f"  `{step}`: {info['calls']} calls, "
            f"{info['prompt_tokens'] + info['completion_tokens']:,} tokens, "
            f"${info['cost']:.4f}"
        )
    return "\n".join(lines)


def format_full_slack_report(sent_count: int, reply_count: int) -> str:
    """Format the overall usage report with by-category breakdown and averages."""
    s = get_all_summary()
    cats = s["categories"]

    lines = [
        "*Overall Token Usage*",
        f"Total campaigns: {s['total_campaigns']}",
        f"Total GPT calls: {s['total_calls']}",
        f"Total tokens: {s['total_tokens']:,}",
        f"Total estimated cost: ${s['total_cost']:.4f}",
        "",
        "*Breakdown by Function:*",
    ]

    # Send pipeline
    c = cats["send"]
    lines.append(
        f"  *Sending emails* (researcher + copywriter + reviewer):"
        f" {c['calls']} calls, {c['tokens']:,} tokens, ${c['cost']:.4f}"
    )
    if sent_count > 0:
        lines.append(
            f"    Avg cost per email sent: {c['tokens'] // sent_count:,} tokens, "
            f"${c['cost'] / sent_count:.4f}"
        )

    # Reply analyzer
    c = cats["reply_analyzer"]
    lines.append(
        f"  *Analyzing replies* (reply_analyzer):"
        f" {c['calls']} calls, {c['tokens']:,} tokens, ${c['cost']:.4f}"
    )
    if reply_count > 0:
        lines.append(
            f"    Avg cost per reply analyzed: {c['tokens'] // reply_count:,} tokens, "
            f"${c['cost'] / reply_count:.4f}"
        )

    # Reporter
    c = cats["reporter"]
    lines.append(
        f"  *Generating insights* (reporter):"
        f" {c['calls']} calls, {c['tokens']:,} tokens, ${c['cost']:.4f}"
    )
    if c["calls"] > 0:
        lines.append(
            f"    Avg cost per report: {c['tokens'] // c['calls']:,} tokens, "
            f"${c['cost'] / c['calls']:.4f}"
        )

    return "\n".join(lines)
