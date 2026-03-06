"""Researcher Agent — analyzes each target company for Telalive outreach."""

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

log = logging.getLogger(__name__)

CACHE_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "research_cache.json")
CACHE_TTL_DAYS = int(os.getenv("RESEARCH_CACHE_TTL_DAYS", "30"))
MODEL = os.getenv("AGENT_MODEL", "gpt-5")

_client: OpenAI | None = None
_cache_lock = threading.Lock()


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(max_retries=10)
    return _client


# ── Cache (thread-safe) ───────────────────────────────────────────────

def _load_cache() -> dict:
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_cache(cache: dict):
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def _check_cache(domain: str) -> dict | None:
    with _cache_lock:
        cache = _load_cache()
    entry = cache.get(domain)
    if not entry:
        return None
    age_days = (time.time() - entry.get("cached_at", 0)) / 86400
    if age_days > CACHE_TTL_DAYS:
        return None
    return entry


def _write_cache(domain: str, result: dict):
    with _cache_lock:
        cache = _load_cache()
        cache[domain] = {**result, "cached_at": time.time()}
        _save_cache(cache)


# ── Website Fetch ──────────────────────────────────────────────────────

def _extract_domain(website: str) -> str:
    w = website.strip().lower()
    for prefix in ("https://", "http://", "www."):
        w = w.removeprefix(prefix)
    return w.rstrip("/")


def _fetch_website(url: str) -> str:
    """Fetch first 3000 chars of a website for analysis."""
    if not url:
        return ""
    for scheme in ("https://", "http://"):
        try:
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


# ── Research ───────────────────────────────────────────────────────────

def research_company(company: dict, campaign_id: str) -> dict:
    """Research a single company. Returns enriched dict with brief."""
    domain = _extract_domain(company.get("website", ""))

    cached = _check_cache(domain) if domain else None
    if cached:
        log.info("Cache hit for %s", domain)
        return {
            **company,
            "brief": cached["brief"],
            "from_cache": True,
        }

    website_text = _fetch_website(domain) if domain else ""

    user_msg = f"""COMPANY INFO:
- Name: {company.get('company_name', 'Unknown')}
- Website: {company.get('website', 'N/A')}
- Industry: {company.get('industry', 'Unknown')}
- Core Business: {company.get('core_business', 'Unknown')}
- Country: {company.get('country', 'Unknown')}
- City: {company.get('city', 'Unknown')}
- Revenue: {company.get('revenue', 'Unknown')}
- Employees: {company.get('employees', 'Unknown')}

WEBSITE CONTENT (first 3000 chars):
{website_text if website_text else '(Could not fetch website)'}"""

    system_prompt = build_system_prompt("researcher")
    client = _get_client()

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

    usage_tracker.record(campaign_id, "researcher", resp.usage.prompt_tokens, resp.usage.completion_tokens)

    try:
        brief = json.loads(resp.choices[0].message.content)
    except json.JSONDecodeError:
        log.error("Researcher JSON parse failed: %s", resp.choices[0].message.content[:200])
        brief = {"reasoning": "Could not parse GPT response", "pain_point": "", "talking_points": [], "personalization_hooks": []}

    if domain:
        _write_cache(domain, {"brief": brief})

    log.info("Researched %s", company.get("company_name"))

    return {
        **company,
        "brief": brief,
        "from_cache": False,
    }


RESEARCH_MAX_WORKERS = int(os.getenv("PIPELINE_MAX_WORKERS", "2"))


def research_batch(companies: list[dict], campaign_id: str, max_workers: int = RESEARCH_MAX_WORKERS) -> list[dict]:
    """Research all companies in a batch (concurrent)."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    results = [None] * len(companies)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_to_idx = {
            pool.submit(research_company, c, campaign_id): i
            for i, c in enumerate(companies)
        }

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
