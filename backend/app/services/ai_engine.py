import asyncio
import json
import logging
from groq import AsyncGroq, Groq
from ..config import settings

logger = logging.getLogger(__name__)

# Shared clients — created once at module load, reused for all AI calls
_async_client: AsyncGroq | None = None
_sync_client: Groq | None = None

def _get_async_client() -> AsyncGroq:
    global _async_client
    if _async_client is None:
        _async_client = AsyncGroq(api_key=settings.groq_api_key)
    return _async_client

def _get_sync_client() -> Groq:
    global _sync_client
    if _sync_client is None:
        _sync_client = Groq(api_key=settings.groq_api_key)
    return _sync_client


SYSTEM_PROMPT = """You are an on-page SEO analyst. Analyze the supplied page using only the page information.
Return valid JSON with exactly these keys:
search_intent: short string
content_quality_score: number 0-100
topic_coverage_score: number 0-100
suggested_title: string or null
suggested_meta_description: string or null
suggested_headings: array of strings
content_improvements: array of strings
analysis: concise explanation
recommendations: array of objects with recommendation and suggested_fix
Do not invent facts that are not supported by the page.
"""

def _build_prompt(page, rule_results) -> str:
    content = (page.content or "")[:settings.ai_max_content_length]
    rule_summary = [
        {"check": r.check_type, "status": r.status, "score": r.score, "severity": r.severity, "details": r.details}
        for r in rule_results
    ]
    category = getattr(page, "category", "")
    priority = getattr(page, "priority", "")
    severity = getattr(page, "highest_severity", "")
    seo_score = getattr(page, "seo_score", 0)

    return f"""Page URL: {getattr(page, 'url', '')}
SEO Score: {seo_score}/100
Category: {category}
Priority: {priority}
Highest Severity Issue: {severity}
Title: {getattr(page, 'title', '')}
Meta description: {getattr(page, 'meta_description', '')}
H1: {getattr(page, 'h1', '')}
Canonical: {getattr(page, 'canonical_url', '')}
Robots: {getattr(page, 'robots_directive', '')}
Content:
{content}

Deterministic Rule-based Findings:
{json.dumps(rule_summary)}
"""

async def analyze_with_ai_async(page, rule_results, sem: asyncio.Semaphore | None = None) -> dict | None:
    if not settings.ai_enabled or not settings.groq_api_key:
        return None

    prompt = _build_prompt(page, rule_results)
    client = _get_async_client()

    async def _call():
        max_retries = 3
        for attempt in range(max_retries):
            try:
                response = await client.chat.completions.create(
                    model=settings.groq_model,
                    temperature=0.2,
                    response_format={"type": "json_object"},
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                )
                return json.loads(response.choices[0].message.content)
            except Exception as exc:
                exc_str = str(exc)
                if "429" in exc_str or "rate_limit" in exc_str.lower():
                    wait_time = 2.5 * (attempt + 1)
                    logger.info("Groq 429 Rate limit hit for %s, retrying in %.1fs (attempt %d/%d)...", getattr(page, 'url', ''), wait_time, attempt + 1, max_retries)
                    await asyncio.sleep(wait_time)
                else:
                    logger.warning("Async Groq request failed for %s: %s", getattr(page, 'url', ''), exc)
                    return None
        return None

    if sem:
        async with sem:
            return await _call()
    return await _call()

def analyze_with_ai(page, rule_results) -> dict | None:
    if not settings.ai_enabled or not settings.groq_api_key:
        return None

    client = _get_sync_client()
    prompt = _build_prompt(page, rule_results)
    try:
        response = client.chat.completions.create(
            model=settings.groq_model,
            temperature=0.2,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        )
        return json.loads(response.choices[0].message.content)
    except Exception as exc:
        logger.warning("Groq request failed for %s: %s", page.url, exc)
        return None
