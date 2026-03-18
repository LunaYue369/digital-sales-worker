import json
import logging
import os
import re
import threading
import time

import requests
from openai import OpenAI

from agents.soul_loader import build_system_prompt
from services import usage_tracker
from concurrent.futures import ThreadPoolExecutor, as_completed

log = logging.getLogger(__name__)

# 在本地储存科研过的公司的信息
CACHE_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "research_cache.json")
# 30天算是公司调研的过期日期
CACHE_TTL_DAYS = int(os.getenv("RESEARCH_CACHE_TTL_DAYS", "30"))
MODEL = os.getenv("AGENT_MODEL", "gpt-5")
_client: OpenAI | None = None
_cache_lock = threading.Lock()


# 链接OPENAI
def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(max_retries=10)
    return _client

# load本地储存的research_cache，返回一个很大dictionary，包含所有的之前科调研过的公司的信息Dictionary
def _load_cache() -> dict:
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

# 保存一个company research dict到cache
def _save_cache(cache: dict):
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)

# 查看某个公司的domain有没有存在于research_cache里
def _check_cache(domain: str) -> dict | None:
    # 先load research cache
    with _cache_lock:
        cache = _load_cache()
    # 查找这个domain
    entry = cache.get(domain)
    if not entry:
        return None
    # 如果超过30天，就算那次调研过期了，即使domain在也算是过期了，这时候返回None，在research_company会write_cache重新更新新的domain信息
    age_days = (time.time() - entry.get("cached_at", 0)) / 86400
    if age_days > CACHE_TTL_DAYS:
        return None
    return entry

# 储存到cache里
def _write_cache(domain: str, result: dict):
    with _cache_lock:
        # 先打开本地的数据
        cache = _load_cache()
        # 储存domain: {一系列公司信息}
        cache[domain] = {**result, "cached_at": time.time()}
        _save_cache(cache)

# 输入一个网站的url string，抓出网站的domain string
def _extract_domain(website: str) -> str:
    w = website.strip().lower()
    for prefix in ("https://", "http://", "www."):
        w = w.removeprefix(prefix)
    return w.rstrip("/")

# 抓取一个website的信息
def _fetch_website(url: str) -> str:
    if not url:
        return ""
    for scheme in ("https://", "http://"):
        try:
            # 访问html，只取前3k字符
            resp = requests.get(scheme + url, timeout=10, headers={
                "User-Agent": "Mozilla/5.0 (compatible; TeLaLiveBot/1.0)"
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
    domain = _extract_domain(company.get("website", ""))
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
    user_msg = f"""COMPANY INFO:
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
        max_tokens=500,
        response_format={"type": "json_object"},
    )

    usage_tracker.record(user_id, campaign_id, "researcher", resp.usage.prompt_tokens, resp.usage.completion_tokens)

    #以下就是researcher人格会返回的东西，会存入brief
    """
    {
        "reasoning": "<3-5 sentence analysis of why Telalive fits this company — be specific to THIS company, not generic>",
        "pain_point": "<the 2-3 most compelling pain points for them that Telalive solves>",
        "talking_points": ["<point 1>", "<point 2>", "<point 3>"],
        "personalization_hooks": ["<specific detail from their website or business that Nate can reference to show he did his homework>"]
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
    if domain:
        company_info = {k: v for k, v in company.items() if k != "brief"}
        _write_cache(domain, {"brief": brief, "company_info": company_info})

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
