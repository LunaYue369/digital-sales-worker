import logging
import os
import re

from openai import OpenAI

from agents.soul_loader import build_system_prompt
from services import usage_tracker

log = logging.getLogger(__name__)

MODEL = os.getenv("AGENT_MODEL", "gpt-5")

_client: OpenAI | None = None


# 落款签名给user_msg用
SIGNATURE = (
    "Best,\n"
    "Nate Hillyer\n"
    "GMIC AI, Inc.\n"
    "nate@gmic.ai\n"
    "Phone: 657-900-5153\n"
    "https://gmic.ai | https://telalive.us\n"
    "LinkedIn: https://www.linkedin.com/company/gmicaiinc"
)


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(max_retries=10)
    return _client


# coptwriter人格会输出subject和body的格式的string，parse出来
def _parse_email_output(text: str) -> tuple[str, str]:
    lines = text.strip().split("\n")
    subject = ""
    body_start = 0
    for i, line in enumerate(lines):
        if line.lower().startswith("subject:"):
            subject = line[len("subject:"):].strip()
            body_start = i + 1
            break

    body = "\n".join(lines[body_start:]).strip()
    if not subject:
        raise ValueError("Copywriter output missing 'Subject:' line — treat as failed draft")
    return subject, body


# 清理copywriter人格自行添加的开头和落款
def _clean_body(body: str) -> str:
    body = re.sub(r"^(Hi|Hello|Dear|Hey)\s+[^\n]*,?\s*\n*", "", body, flags=re.IGNORECASE).strip()
    body = re.sub(
        r"\n\s*(Best|Regards|Sincerely|Cheers|Warm regards|Kind regards|Thanks|Thank you)[,.]?\s*\n.*",
        "", body, flags=re.IGNORECASE | re.DOTALL
    ).strip()
    body = re.sub(r"\n\s*P\.?S\.?[:\s].*", "", body, flags=re.IGNORECASE | re.DOTALL).strip()
    return body


# 保证body分自然段
def _ensure_paragraphs(body: str) -> str:
    if "\n\n" in body:
        return body
    sentences = re.split(r"(?<=\.)\s+", body)
    if len(sentences) <= 2:
        return body
    paragraphs = []
    for i in range(0, len(sentences), 2):
        paragraphs.append(" ".join(sentences[i:i + 2]))
    return "\n\n".join(paragraphs)


# copywriter初次写或者重写邮件，返回 subject str，body str，tokens消耗字典
def write_email(company: dict, campaign_id: str, feedback: str = "", previous_email: str = "") -> tuple[str, str, dict]:
    # research人格查出来的brief
    brief = company.get("brief", {})
    # 加载copywriter人格
    system_prompt = build_system_prompt("copywriter")
    client = _get_client()
    company_name = company.get("company_name", "")
    # 定制化开头
    hooks = brief.get("personalization_hooks", [])

    # 如果有feedback string，那就证明是要重写
    if feedback and previous_email:
        # 重写email的提示词，带上 research 数据，避免重写时丢失个性化信息
        user_msg = (
            f"TARGET COMPANY: {company_name} ({company.get('industry', '')}, {company.get('core_business', '')})\n"
            f"- Pain Point: {brief.get('pain_point', '')}\n"
            f"- Talking Points: {', '.join(brief.get('talking_points', []))}\n"
            f"- Personalization Hooks: {', '.join(hooks) if hooks else 'None'}\n\n"
            f"YOUR PREVIOUS EMAIL WAS REJECTED. Reviewer feedback:\n{feedback}\n\n"
            f"PREVIOUS EMAIL:\n{previous_email}\n\n"
            f"Rewrite the subject line AND email body (no greeting, no signature — those are added by code).\n"
            f"Address ALL feedback points. You are Nate Hillyer from GMIC AI writing TO this company."
        )
    else:
        user_msg = (
            f"TARGET COMPANY RESEARCH:\n"
            f"- Company Name: {company_name}\n"
            f"- Industry: {company.get('industry', '')}\n"
            f"- Core Business: {company.get('core_business', '')}\n"
            f"- Country/City: {company.get('country', '')}, {company.get('city', '')}\n"
            f"- Website: {company.get('website', '')}\n"
            f"- Revenue: {company.get('revenue', '')}\n"
            f"- Employees: {company.get('employees', '')}\n"
            f"- Pain Point: {brief.get('pain_point', '')}\n"
            f"- Talking Points: {', '.join(brief.get('talking_points', []))}\n"
            f"- Research Reasoning: {brief.get('reasoning', '')}\n"
            f"- Personalization Hooks: {', '.join(hooks) if hooks else 'None'}\n\n"
            f"Write ONLY the subject line and email body. Do NOT include a greeting (Hi/Dear) or signature — those are added automatically by code.\n"
            f"You are Nate Hillyer from GMIC AI writing TO {company_name}."
        )

    # 发送给Copywriter模型我们的需求
    resp = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ],

        temperature=0.5,
        max_tokens=600,
    )

    pt = resp.usage.prompt_tokens
    ct = resp.usage.completion_tokens
    usage_tracker.record(campaign_id, "copywriter", pt, ct)

    # parse GPT返回的结果，一段string，包含subject和body
    subject, raw_body = _parse_email_output(resp.choices[0].message.content)

    # 防御性措施，不写到人格.md里
    # 清理body里出现的打招呼和落款
    clean = _clean_body(raw_body)
    # 保证Body分自然段
    clean = _ensure_paragraphs(clean)
    # 添加hardcode打招呼+hardcode落款
    greeting = f"Hi {company_name} team,\n\n"
    body = f"{greeting}{clean}\n\n{SIGNATURE}"

    return subject, body, {"prompt": pt, "completion": ct}
