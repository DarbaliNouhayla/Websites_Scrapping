"""
severity.py — Groq LLM-based severity classification with cache + exponential backoff.

CISA exception: uses CVSS score directly (objective, no LLM needed).
All other sources: Groq llama-3.3-70b-versatile via JSON mode.

Architecture:
  classify_severity()      → checks cache → Groq API with tenacity retry → stores in cache
  severity_from_cvss()     → sync, no cache needed (deterministic math)
  get_severity()           → router: CVSS for CISA, Groq for everything else
  classify_batch()         → batch processor with rate limiting + cache pass-through
"""

import asyncio
import json
import logging
import os
from typing import Protocol

from dotenv import load_dotenv
from groq import AsyncGroq
from httpx import HTTPStatusError
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

load_dotenv()

log = logging.getLogger(__name__)


class SeverityCacheProtocol(Protocol):
    """Duck-typed protocol — matches SeverityCache & CheckpointStore."""

    def get(self, source: str, title: str, description: str) -> dict | None: ...
    def set(self, source: str, title: str, description: str, result: dict) -> None: ...

_client: AsyncGroq | None = None
_cache: SeverityCacheProtocol | None = None


def _get_client() -> AsyncGroq:
    global _client
    if _client is None:
        api_key = os.getenv("GROQ_API_KEY")
        timeout=180.0
        if not api_key:
            raise ValueError(
                "GROQ_API_KEY not set. Add it to your .env file: GROQ_API_KEY=gsk_..."
            )
        _client = AsyncGroq(api_key=api_key)
    return _client


def _get_cache(db_path: str | None = None) -> SeverityCacheProtocol:
    global _cache
    if _cache is None:
        from financial_scraper.utils.cache import SeverityCache
        _cache = SeverityCache(db_path=db_path)
    return _cache


_FALLBACK = {"severity": "medium", "reason": "Classification unavailable"}

_SYSTEM_PROMPT = """
You are a risk analyst for Attijariwafa Bank, the largest bank in Morocco.
You classify scraped content severity for a Third-Party Risk Management platform.

Given a source name, article title and description, return a JSON object with:
- "severity": exactly one of "critical", "high", "medium", "low"
- "reason": one short sentence explaining why, written in the same language as the article

Severity definitions:
- critical: regulatory sanctions/fines/license suspension/enforcement actions,
            actively exploited zero-day cyberattacks, national strikes blocking
            supply chains, systemic financial crises directly affecting Morocco
- high: new binding regulations with compliance deadlines, central bank rate
        decisions, significant Morocco/Africa economic impact, high-severity
        unpatched vulnerabilities, major supply chain disruptions
- medium: regulatory reports/statistics/consultations, moderate market news,
          patch advisories with no active exploitation, economic forecasts,
          international institution publications without direct Morocco impact
- low: personnel appointments, partnership announcements, routine daily
       publications, informational notices, training sessions, press releases
       without operational impact

Return ONLY valid JSON. No text outside the JSON object.
"""


def severity_from_cvss(score: float | None) -> dict:
    if score is None:
        return {"severity": "medium", "reason": "CVSS score unknown — defaulting to medium"}
    if score >= 9.0:
        return {"severity": "critical", "reason": f"CVSS score {score} — critical severity"}
    if score >= 7.0:
        return {"severity": "high", "reason": f"CVSS score {score} — high severity"}
    if score >= 4.0:
        return {"severity": "medium", "reason": f"CVSS score {score} — medium severity"}
    return {"severity": "low", "reason": f"CVSS score {score} — low severity"}


def _is_rate_limit_error(exc: BaseException) -> bool:
    """Return True if exc is an HTTP 429 Too Many Requests."""
    if isinstance(exc, HTTPStatusError) and exc.response.status_code == 429:
        return True
    # Groq wraps 429 in its own exception type
    err_str = str(exc)
    if "429" in err_str or "rate limit" in err_str.lower() or "too many requests" in err_str.lower():
        return True
    return False


_RETRY_DECORATOR = retry(
    retry=retry_if_exception(_is_rate_limit_error),
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=60, min=60, max=300),
    reraise=True,
)


@_RETRY_DECORATOR
async def _call_groq(user_content: str) -> str:
    """Single Groq API call with tenacity retry for 429 rate limits."""
    response = await _get_client().chat.completions.create(
        #model="llama-3.3-70b-versatile",
        model="llama-3.1-8b-instant",
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        max_tokens=80,
        temperature=0,
        response_format={"type": "json_object"},
    )
    return response.choices[0].message.content.strip()


async def classify_severity(
    title: str,
    description: str = "",
    source: str = "",
    cache: SeverityCacheProtocol | None = None,
) -> dict:
    """Classify severity of a single article.

    1. Check checkpoint/cache — skip Groq if already classified.
    2. Call Groq with tenacity retry for 429 rate limits.
    3. Store result in checkpoint/cache on success.

    Args:
        title: Article title
        description: Article description/summary (first 300 chars used)
        source: Source name e.g. "BAM", "Médias24", "World Bank"
        cache: CheckpointStore or SeverityCache instance. If omitted, no caching.

    Returns:
        {"severity": "critical|high|medium|low", "reason": "explanation"}
    """
    # 1. Check checkpoint/cache — skip Groq if already classified
    if cache is not None:
        cached = cache.get(source, title, description)
        if cached is not None:
            log.debug("[severity] Cache/checkpoint HIT for '%s'", title[:60])
            return cached

    user_content = (
        f"Source: {source}\n"
        f"Title: {title[:200]}\n"
        f"Description: {description[:150]}"
    ).strip()

    try:
        raw = await _call_groq(user_content)
        result = json.loads(raw)

        if result.get("severity") not in ("critical", "high", "medium", "low"):
            log.warning(
                "[severity] Invalid severity value '%s' for: %s",
                result.get("severity"), title[:60]
            )
            result["severity"] = "medium"

        if not result.get("reason"):
            result["reason"] = f"Classified by Groq as {result['severity']}"

        # 2. Persist to checkpoint/cache immediately on success
        if cache is not None:
            cache.set(source, title, description, result)

        return result

    except json.JSONDecodeError as exc:
        log.warning("[severity] JSON parse failed for '%s': %s", title[:60], exc)
        return _FALLBACK
    except Exception as exc:
        err_str = str(exc)

        # Retry once with minimal input on json_validate_failed / max tokens
        if "json_validate_failed" in err_str or "max completion tokens" in err_str:
            log.warning("[severity] Token overflow for '%s' \u2014 retrying with minimal input", title[:40])
            try:
                response = await _get_client().chat.completions.create(
                    model="llama-3.1-8b-instant",
                    messages=[
                        {"role": "system", "content": _SYSTEM_PROMPT},
                        {"role": "user", "content": f"Source: {source}\nTitle: {title[:100]}"},
                    ],
                    max_tokens=80,
                    temperature=0,
                    response_format={"type": "json_object"},
                )
                raw = response.choices[0].message.content.strip()
                result = json.loads(raw)
                if result.get("severity") in ("critical", "high", "medium", "low"):
                    if cache is not None:
                        cache.set(source, title, description, result)
                    return result
            except Exception:
                pass

        log.warning("[severity] Groq call failed for '%s': %s", title[:60], exc)
        return _FALLBACK


async def get_severity(
    item: dict,
    cache: SeverityCacheProtocol | None = None,
) -> dict:
    """Main router — decides whether to use CVSS or Groq based on source.

    Args:
        item: dict with keys: title, description, source, cve_score (optional)
        cache: Optional SeverityCache instance.

    Returns:
        {"severity": "critical|high|medium|low", "reason": "explanation"}
    """
    cve_score = item.get("cve_score")
    source = item.get("source", "")

    # CISA exception — use CVSS score, never LLM
    if source in ("CISA", "cisa") and cve_score is not None:
        return severity_from_cvss(cve_score)

    # Fast test mode — skip Groq entirely
    import os
    if os.environ.get("SKIP_GROQ"):
        return {"severity": "medium", "reason": "SKIP_GROQ mode"}

    return await classify_severity(
        title=item.get("title", ""),
        description=item.get("description", ""),
        source=source,
        cache=cache,
    )


async def classify_batch(
    items: list[dict],
    cache: SeverityCacheProtocol | None = None,
) -> list[dict]:
    """Classify a list of items with built-in rate limiting and caching.

    Groq free tier: 30 requests/minute = 1 request per 2 seconds.
    Cache hits bypass both the API call and the rate-limit sleep.

    Args:
        items: list of dicts, each with: title, description, source, cve_score
        cache: Optional SeverityCache instance.

    Returns:
        list of {"severity": "...", "reason": "..."} in same order as input
    """
    results = []
    total = len(items)
    cache_hits = 0

    for i, item in enumerate(items):
        source = item.get("source", "")
        title = item.get("title", "")
        description = item.get("description", "")

        # Pre-check cache to skip rate-limit sleep for cache hits
        is_cached = False
        if cache is not None:
            cached = cache.get(source, title, description)
            if cached is not None:
                results.append(cached)
                cache_hits += 1
                is_cached = True

        if not is_cached:
            result = await get_severity(item, cache=cache)
            results.append(result)
        else:
            result = results[-1]

        log.debug(
            "[severity] %d/%d — %s → %s%s",
            i + 1, total,
            title[:50], result["severity"],
            " (cache)" if is_cached else "",
        )

        # Rate limiting: 2.1s between Groq API calls
        # Skip sleep on last item, CVSS-based classifications, and cache hits
        is_last = (i == total - 1)
        is_cvss = (source in ("CISA", "cisa") and item.get("cve_score") is not None)
        if not is_last and not is_cvss and not is_cached:
            await asyncio.sleep(2.1)

    log.info(
        "[severity] Classified %d items (%d cache hits, %d API calls)",
        total, cache_hits, total - cache_hits,
    )
    return results


if __name__ == "__main__":
    import asyncio
    import json
    logging.basicConfig(level=logging.INFO)
    log.info("[severity] Module loaded OK — GROQ_API_KEY=%s", "set" if os.getenv("GROQ_API_KEY") else "NOT SET")

    async def _test():
        r1 = await classify_severity(
            title="BAM suspend l'agrément d'une banque",
            description="La banque centrale du Maroc a décidé de suspendre l'agrément d'une banque pour non-respect des réglementations.",
            source="BAM",
        )
        print("Test 1:", json.dumps(r1, ensure_ascii=False))

        r2 = await classify_severity(
            title="Transferts MRE progressent de 8.8%",
            description="Selon l'Office des changes, les transferts des MRE ont augmenté de 8.8% au premier semestre.",
            source="Médias24",
        )
        print("Test 2:", json.dumps(r2, ensure_ascii=False))

        r3 = await classify_severity(
            title="ECB raises rates by 25 basis points",
            description="The European Central Bank decided today to raise interest rates by 25 basis points.",
            source="ECB",
        )
        print("Test 3:", json.dumps(r3, ensure_ascii=False))

        cv = severity_from_cvss(9.8)
        print("Test CVSS 9.8:", json.dumps(cv, ensure_ascii=False))

        cv = severity_from_cvss(None)
        print("Test CVSS None:", json.dumps(cv, ensure_ascii=False))

    asyncio.run(_test())
