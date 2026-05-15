"""Reporter Agent — generates campaign insights and copywriter feedback from stats."""

import json
import logging
import os

from openai import OpenAI

from agents.soul_loader import build_system_prompt
from services import usage_tracker

log = logging.getLogger(__name__)

MODEL = os.getenv("AGENT_MODEL", "gpt-5")

_client: OpenAI | None = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(max_retries=10)
    return _client


def generate_insights(stats_summary: dict, user_id: str = "") -> dict:
    """
    Generate insights from aggregated campaign stats.
    stats_summary should include: reply_rate, industry_breakdown,
    subject_performance, length_analysis, timing_data, sentiment_summary, etc.
    """
    system_prompt = build_system_prompt("reporter", user_id)
    client = _get_client()

    user_msg = (
        "Here are the aggregated campaign statistics. Analyze them and provide insights.\n\n"
        f"```json\n{json.dumps(stats_summary, ensure_ascii=False, indent=2)}\n```"
    )

    resp = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ],

        temperature=0.3,
        max_tokens=1500,
        response_format={"type": "json_object"},
    )

    usage_tracker.record(user_id, "reporter", "reporter", resp.usage.prompt_tokens, resp.usage.completion_tokens)

    try:
        return json.loads(resp.choices[0].message.content)
    except json.JSONDecodeError:
        log.error("Reporter JSON parse failed: %s", resp.choices[0].message.content[:200])
        return {"overall_summary": "Could not generate insights.", "copywriter_feedback": []}
