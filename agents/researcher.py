import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone

import requests
from openai import OpenAI

from agents.soul_loader import build_system_prompt
from services import usage_tracker
from services.email_finder import extract_domain
from services.firestore_client import research_cache_col
from concurrent.futures import ThreadPoolExecutor, as_completed

log = logging.getLogger(__name__)

# 30天算是公司调研的过期日期
CACHE_TTL_DAYS = int(os.getenv("RESEARCH_CACHE_TTL_DAYS", "30"))
MODEL = os.getenv("AGENT_MODEL", "gpt-5")
_client: OpenAI | None = None


# 链接OPENAI
def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(max_retries=10)
    return _client

# 查看某个公司的domain有没有存在于research_cache里
def _check_cache(domain: str) -> dict | None:
    """Return cached brief+company_info for `domain` if present and not expired,
    else None. Backed by Firestore `research_cache/{domain}`; expiry is judged
    against the doc's `expires_at` (set to cached_at + CACHE_TTL_DAYS at write)."""
    doc = research_cache_col().document(domain).get()
    if not doc.exists:
        return None
    entry = doc.to_dict()
    expires_at = entry.get("expires_at")
    if expires_at and expires_at < datetime.now(tz=timezone.utc):
        return None
    return {"brief": entry["brief"], "company_info": entry.get("company_info", {})}


# 储存到cache里
def _write_cache(domain: str, result: dict):
    """Write a research brief to Firestore. Caller is expected to have validated
    `result['brief']` with _is_brief_valid before calling — bad briefs poison
    the cache for CACHE_TTL_DAYS."""
    now = datetime.now(tz=timezone.utc)
    research_cache_col().document(domain).set({
        "domain": domain,
        "brief": result["brief"],
        "company_info": result.get("company_info", {}),
        "cached_at": now,
        "expires_at": now + timedelta(days=CACHE_TTL_DAYS),
    })


# 校验 brief 质量是否值得 cache —— 避免 GPT 解析失败 / 空 brief 占位 30 天
def _is_brief_valid(brief: dict) -> bool:
    if not isinstance(brief, dict):
        return False
    reasoning = (brief.get("reasoning") or "").strip()
    pain_point = (brief.get("pain_point") or "").strip()
    talking_points = brief.get("talking_points") or []
    # JSON parse 失败的兜底文本
    if "Could not parse" in reasoning:
        return False
    # reasoning 太短说明 GPT 没认真分析
    if len(reasoning) < 30:
        return False
    if not pain_point:
        return False
    # talking_points 必须是非空 list
    if not isinstance(talking_points, list) or not talking_points:
        return False
    return True

# 抓取一个website的信息
def _fetch_website(url: str) -> str:
    if not url:
        return ""
    for scheme in ("https://", "http://"):
        try:
            # 访问html，只取前3k字符
            resp = requests.get(scheme + url, timeout=10, headers={
                "User-Agent": "Mozilla/5.0 (compatible; SalesBot/1.0)"
            })
            if resp.ok:
                text = resp.text
                text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL | re.IGNORECASE)
                text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
                text = re.sub(r"<[^>]+>", " ", text)
                text = re.sub(r"\s+", " ", text).strip()
                return text[:3000]
        except Exception:
            continue
    return ""

# 输入某一个公司dataframe的dict，返回对该公司进行网络调查的dict
def research_company(company: dict, campaign_id: str, user_id: str = "") -> dict:
    # 找出domain string
    domain = extract_domain(company.get("website", ""))
    # 检查对该公司的调查是否已经在本地储存超过30天
    cached = _check_cache(domain) if domain else None
    # 如果没有超过30天，就不要重新再scrape了，直接用现有的company信息
    if cached:
        log.info("Cache hit for %s", domain)
        return {
            **company,
            "brief": cached["brief"],
            "from_cache": True,
        }
    # scrape网站获取的string
    website_text = _fetch_website(domain) if domain else ""
    
    # 把参数company的所有信息解析成json object的样子
    # 再加上fetch到的website数据，用user的身份发给GPT的researcher人格
    user_msg = f"""Return ONLY a JSON object with these four keys: "reasoning", "pain_point", "talking_points", "personalization_hooks". Do NOT wrap in any other key. Do NOT echo the company info back.

COMPANY INFO:
                - Name: {company.get('company_name', 'Unknown')}
                - Website: {company.get('website', 'N/A')}
                - Industry: {company.get('industry', 'Unknown')}
                - Core Business: {company.get('core_business', 'Unknown')}
                - Country: {company.get('country', 'Unknown')}
                - State: {company.get('state', 'Unknown')}
                - City: {company.get('city', 'Unknown')}
                - Revenue: {company.get('revenue', 'Unknown')}
                - Employees: {company.get('employees', 'Unknown')}
                - Phone: {company.get('phone', 'Unknown')}
                - Google Rating: {company.get('review_rating', 'Unknown')}
                - Review Count: {company.get('review_count', 'Unknown')}
                WEBSITE CONTENT (first 3000 chars):
                {website_text if website_text else '(Could not fetch website)'}"""
    # 获取researcher的_shared+独立人格的string
    system_prompt = build_system_prompt("researcher", user_id)
    # call GPT model
    client = _get_client()
    # GPT用researcher人格接受和回答问题
    # 我们用user人格传入user_msg信息
    resp = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ],

        temperature=0.3,
        max_tokens=1200,
        response_format={"type": "json_object"},
    )

    usage_tracker.record(user_id, campaign_id, "researcher", resp.usage.prompt_tokens, resp.usage.completion_tokens)

    #以下就是researcher人格会返回的东西，会存入brief
    """
    {
        "reasoning": "<3-5 sentence analysis of why PhonePilot fits this company — be specific to THIS company, not generic>",
        "pain_point": "<the 2-3 most compelling pain points for them that PhonePilot solves>",
        "talking_points": ["<point 1>", "<point 2>", "<point 3>"],
        "personalization_hooks": ["<specific detail from their website or business that the rep can reference to show they did their homework>"]
        }
    """
    try:
        brief = json.loads(resp.choices[0].message.content)
    except json.JSONDecodeError:
        log.error("Researcher JSON parse failed: %s", resp.choices[0].message.content[:200])
        brief = {"reasoning": "Could not parse GPT response", "pain_point": "", "talking_points": [], "personalization_hooks": []}

    # 最后储存在本地research_cache里的东西是公司的dataframe的信息+GPT根据公司df信息和fetchwebsite得到的brief总结
    """
    {
        "joespizza.com": {
        "brief": {
            "reasoning": "...",
            "pain_point": "...",
            "talking_points": [...],
            "personalization_hooks": [...]
        },
        "company_info": {
            "company_name": "Joe's Pizza",
            "contact_email": "joe@joespizza.com",
            "website": "joespizza.com",
            "industry": "Restaurant",
            "core_business": "Pizza restaurant",
            "country": "US",
            "city": "New York",
            "revenue": "$1M-5M",
            "employees": "10-50"
        },
        "cached_at": 1741234567.89
        }
    }
    """
    if domain and _is_brief_valid(brief):
        company_info = {k: v for k, v in company.items() if k != "brief"}
        _write_cache(domain, {"brief": brief, "company_info": company_info})
    elif domain:
        log.warning("Skipping cache write for %s — brief failed validation (likely GPT parse failure or empty fields)", domain)

    log.info("Researched %s", company.get("company_name"))

    # 返回的是一个超级完整的大Dictionary包含公司所有的df信息和brief卖点，是要给copywriter人格使用的
    return {
        **company,
        "brief": brief,
        "from_cache": False,
    }


# 最多并发运行多少threads
RESEARCH_MAX_WORKERS = int(os.getenv("PIPELINE_MAX_WORKERS", "3"))

# research一个File上的一组公司，并发进行research，返回一个List of Dictionary，每个Dictionary是每个公司的信息，包含公司在表格上的基本信息和brief
def research_batch(companies: list[dict], campaign_id: str, user_id: str = "", max_workers: int = RESEARCH_MAX_WORKERS) -> list[dict]:
    results = [None] * len(companies)

    # 并发一次性RESEARCH_MAX_WORKERS个线程，同时跑research_company
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_to_idx = {
            pool.submit(research_company, c, campaign_id, user_id): i
            for i, c in enumerate(companies)
        }
        # 按顺序把结果research结果加入到results里
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            c = companies[idx]
            try:
                results[idx] = future.result()
            except Exception as e:
                log.error("Research failed for %s: %s", c.get("company_name"), e)
                results[idx] = {
                    **c,
                    "brief": {"reasoning": f"Research error: {e}", "pain_point": "", "talking_points": [], "personalization_hooks": []},
                }
    return results
