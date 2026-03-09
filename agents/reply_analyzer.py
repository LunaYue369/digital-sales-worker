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


# Reply Analyzer Agent — 使用 GPT 深度分析首次回复邮件。
def analyze_reply(reply_record: dict) -> dict:
    # 加载人格
    system_prompt = build_system_prompt("reply_analyzer")
    client = _get_client()

    # 构建用户消息：给 reply_analyzer 提供原始邮件上下文 + 回复内容
    user_msg = (
        f"ORIGINAL EMAIL:\n"
        f"- To: {reply_record.get('company_name', '')} ({reply_record.get('industry', '')})\n"
        f"- Subject: {reply_record.get('original_subject', '')}\n\n"
        f"REPLY FROM: {reply_record.get('reply_from', '')}\n"
        f"REPLY BODY:\n{reply_record.get('reply_body', '')}"
    )
    
    # 调用人格
    resp = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ],

        temperature=0.2,
        max_tokens=800,
        response_format={"type": "json_object"},
    )

    # 记录 token 用量
    campaign_id = reply_record.get("campaign_id", "tracking")
    usage_tracker.record(campaign_id, "reply_analyzer", resp.usage.prompt_tokens, resp.usage.completion_tokens)

    try:
        analysis = json.loads(resp.choices[0].message.content)
    except json.JSONDecodeError:
        log.error("Reply analyzer JSON 解析失败: %s", resp.choices[0].message.content[:200])
        # 兜底默认值，确保下游不会因缺少字段而报错
        analysis = {
            "sentiment": "neutral",
            "intent": "other",
            "reason_summary": "Could not parse reply.",
            "why_accepted_or_rejected": "Analysis failed.",
            "follow_up_advice": "Review the reply manually and respond accordingly.",
            "improvement_tip": "N/A.",
            "company_factors": {
                "industry": reply_record.get("industry", "unknown"),
                "size_signal": "unknown",
                "pain_point": None,
                "current_solution": None,
            },
            "objections": [],
            "questions_asked": [],
            "is_decision_maker": False,
            "referral": None,
            "summary": "Could not analyze reply.",
        }

    return analysis
