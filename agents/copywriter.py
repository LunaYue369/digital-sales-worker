import logging
import os
import re

from openai import OpenAI

from agents.soul_loader import build_system_prompt
from services import usage_tracker
from core.user_config import get_user_config, get_template_config

log = logging.getLogger(__name__)

MODEL = os.getenv("AGENT_MODEL", "gpt-5")

_client: OpenAI | None = None


# 默认落款签名（fallback if user has no per-user config）
DEFAULT_SIGNATURE = (
    "Best,\n"
    "GMIC AI, Inc.\n"
    "https://gmic.ai | https://telalive.us"
)


def _get_user_signature(user_id: str, template: str = "default") -> str:
    """Get per-user signature, checking template-specific config first."""
    if user_id:
        tpl = get_template_config(user_id, template)
        if tpl.get("signature"):
            return tpl["signature"]
    return DEFAULT_SIGNATURE


def _get_user_greeting(user_id: str, company_name: str, contact_name: str = "", template: str = "default") -> str:
    """Get per-user greeting style, checking template-specific config first.
    Supports {contact} (contact person name) and {company} placeholders.
    """
    style = None
    if user_id:
        tpl = get_template_config(user_id, template)
        style = tpl.get("greeting_style")

    if style:
        if "{contact}" in style:
            name = contact_name if contact_name else f"{company_name} team"
            style = style.replace("{contact}", name)
        style = style.replace("{company}", company_name)
        return style + "\n\n"
    return f"Hi {company_name} team,\n\n"


def _get_sender_identity(user_id: str) -> str:
    """Get sender name + company for GPT prompt."""
    config = get_user_config(user_id) if user_id else None
    if config and config.get("name"):
        return f"{config['name']} from GMIC AI"
    return "a salesperson from GMIC AI"


def _write_static_email(company: dict, user_id: str, tpl: dict) -> tuple[str, str, dict]:
    """Static template — no GPT call, just string interpolation."""
    contact_name = company.get("contact_name", "")
    first_name = company.get("first_name", "")
    if not first_name and contact_name:
        first_name = contact_name.split()[0]
    last_name = company.get("last_name", "")
    company_name = company.get("company_name", "")

    subject = tpl.get("static_subject", "Quick question")
    body = tpl["static_body"]
    # Capitalize names in case source data has inconsistent casing
    first_name = first_name.capitalize() if first_name else ""
    last_name = last_name.capitalize() if last_name else ""
    contact_name = " ".join(w.capitalize() for w in contact_name.split()) if contact_name else ""

    for placeholder, value in [
        ("{first_name}", first_name),
        ("{last_name}", last_name),
        ("{contact_name}", contact_name),
        ("{company_name}", company_name),
    ]:
        body = body.replace(placeholder, value)

    signature = tpl.get("signature", DEFAULT_SIGNATURE)
    full_body = f"{body}\n\n{signature}"
    return subject, full_body, {"prompt": 0, "completion": 0}


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
def write_email(company: dict, campaign_id: str, user_id: str = "", feedback: str = "", previous_email: str = "", template: str = "default") -> tuple[str, str, dict]:
    # 静态模板模式 — 跳过 GPT，直接套名字
    if user_id:
        tpl = get_template_config(user_id, template)
        if tpl.get("static_body"):
            return _write_static_email(company, user_id, tpl)

    # research人格查出来的brief
    brief = company.get("brief", {})
    # 加载copywriter人格（支持模板选择）
    system_prompt = build_system_prompt("copywriter", user_id, template)
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
            f"Address ALL feedback points. You are {_get_sender_identity(user_id)} writing TO this company."
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
            f"You are {_get_sender_identity(user_id)} writing TO {company_name}."
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
    usage_tracker.record(user_id, campaign_id, "copywriter", pt, ct)

    # parse GPT返回的结果，一段string，包含subject和body
    subject, raw_body = _parse_email_output(resp.choices[0].message.content)

    # 防御性措施，不写到人格.md里
    # 清理body里出现的打招呼和落款
    clean = _clean_body(raw_body)
    # 保证Body分自然段
    clean = _ensure_paragraphs(clean)
    # 添加 per-user 打招呼+落款（支持模板选择）
    contact_name = company.get("contact_name", "")
    greeting = _get_user_greeting(user_id, company_name, contact_name, template)
    signature = _get_user_signature(user_id, template)
    body = f"{greeting}{clean}\n\n{signature}"

    return subject, body, {"prompt": pt, "completion": ct}
