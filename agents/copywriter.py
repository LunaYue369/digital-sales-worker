"""Copywriter Agent — writes a unique cold email for every company."""

import logging
import os
import re

from openai import OpenAI

from agents.soul_loader import build_system_prompt
from services import usage_tracker

log = logging.getLogger(__name__)

MODEL = os.getenv("AGENT_MODEL", "gpt-5")

_client: OpenAI | None = None

# Hardcoded signature — GPT does NOT generate this. Code appends it.
SIGNATURE = (
    "Best,\n"
    "Nate Hillyer\n"
    "GMIC AI, Inc.\n"
    "nate@gmic.ai\n"
    "Phone: 657-900-5153\n"
    "https://gmic.ai | https://telalive.ai\n"
    "LinkedIn: https://www.linkedin.com/company/gmicaiinc"
)


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(max_retries=10)
    return _client


def _parse_email_output(text: str) -> tuple[str, str]:
    """Parse GPT output into (subject, body)."""
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
        subject = lines[0] if lines else "Quick question about your phone setup"
    return subject, body


def _clean_body(body: str) -> str:
    """Remove any greeting or signature GPT may have added — code handles those."""
    # Remove greeting lines (Hi X, Hello X, Dear X, Hey X)
    body = re.sub(r"^(Hi|Hello|Dear|Hey)\s+[^\n]*,?\s*\n*", "", body, flags=re.IGNORECASE).strip()
    # Remove trailing signature block (everything after "Best," / "Regards," / "Sincerely," etc.)
    body = re.sub(
        r"\n\s*(Best|Regards|Sincerely|Cheers|Warm regards|Kind regards|Thanks|Thank you)[,.]?\s*\n.*",
        "", body, flags=re.IGNORECASE | re.DOTALL
    ).strip()
    # Remove any P.S. lines
    body = re.sub(r"\n\s*P\.?S\.?[:\s].*", "", body, flags=re.IGNORECASE | re.DOTALL).strip()
    return body


def write_email(company: dict, campaign_id: str, feedback: str = "", previous_email: str = "") -> tuple[str, str, dict]:
    """
    Write or rewrite an email for a single company.
    Returns: (subject, body, token_usage)
    """
    brief = company.get("brief", {})
    system_prompt = build_system_prompt("copywriter")
    client = _get_client()

    company_name = company.get("company_name", "")
    hooks = brief.get("personalization_hooks", [])

    if feedback and previous_email:
        user_msg = (
            f"TARGET COMPANY: {company_name} ({company.get('industry', '')}, {company.get('core_business', '')})\n\n"
            f"YOUR PREVIOUS EMAIL WAS REJECTED. Reviewer feedback:\n{feedback}\n\n"
            f"PREVIOUS EMAIL:\n{previous_email}\n\n"
            f"Rewrite ONLY the email body (no greeting, no signature — those are added by code).\n"
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

    subject, raw_body = _parse_email_output(resp.choices[0].message.content)

    # Clean any greeting/signature GPT added despite instructions
    clean = _clean_body(raw_body)

    # Assemble final email: hardcoded greeting + GPT body + hardcoded signature
    greeting = f"Hi {company_name},\n\n"
    body = f"{greeting}{clean}\n\n{SIGNATURE}"

    return subject, body, {"prompt": pt, "completion": ct}
