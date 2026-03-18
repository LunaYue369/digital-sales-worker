"""Reviewer Agent — strict 3-round quality review for each customized email."""

import json
import logging
import os

from openai import OpenAI

from agents.soul_loader import build_system_prompt
from services import usage_tracker

log = logging.getLogger(__name__)

MODEL = os.getenv("AGENT_MODEL", "gpt-5")
MAX_ROUNDS = int(os.getenv("REVIEWER_MAX_ROUNDS", "3"))

_client: OpenAI | None = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(max_retries=10)
    return _client


# reviewer审核email，返回分数字典
def review_email(company: dict, subject: str, body: str, campaign_id: str, user_id: str = "") -> dict:
    # 加载人格
    system_prompt = build_system_prompt("reviewer", user_id)
    client = _get_client()

    # 我们的要求
    user_msg = (
        f"TARGET COMPANY: {company.get('company_name', '')} "
        f"({company.get('industry', '')}, {company.get('core_business', '')})\n\n"
        f"EMAIL TO REVIEW:\n"
        f"Subject: {subject}\n\n"
        f"{body}"
    )

    # 让reviewer工作
    resp = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ],

        temperature=0.2,
        max_tokens=600,
        response_format={"type": "json_object"},
    )

    usage_tracker.record(user_id, campaign_id, "reviewer", resp.usage.prompt_tokens, resp.usage.completion_tokens)

    try:
        # 返回打分字典
        result = json.loads(resp.choices[0].message.content)
    except json.JSONDecodeError:
        log.error("Reviewer JSON parse failed: %s", resp.choices[0].message.content[:200])
        result = {"approved": False, "verdict": "Could not parse review", "scores": {}, "critical_issues": [], "suggestions": []}

    # Log review details for debugging
    company_name = company.get("company_name", "?")
    approved = result.get("approved", False)
    scores = result.get("scores", {})
    log.info("Review %s: approved=%s scores=%s critical=%s",
             company_name, approved, scores, result.get("critical_issues", []))

    return result


# 对于不合格的邮件，写feedback，其实就是 把review_email里返回的打分Dict给String化
def build_feedback(review: dict) -> str:
    parts = []
    if review.get("critical_issues"):
        parts.append("CRITICAL ISSUES:\n" + "\n".join(f"- {i}" for i in review["critical_issues"]))
    if review.get("suggestions"):
        parts.append("SUGGESTIONS:\n" + "\n".join(f"- {s}" for s in review["suggestions"]))
    if review.get("verdict"):
        parts.append(f"VERDICT: {review['verdict']}")
    return "\n\n".join(parts)
