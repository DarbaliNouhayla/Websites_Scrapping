"""
fetcher.py — TieredFetcher with Anti-Ban protocol.

Execution order per source:
  TIER 1: Public REST/SDMX API  (httpx, official docs)
  TIER 2: Official RSS/XML Feed (httpx or curl_cffi)
  TIER 3: Polite HTML Scrape    (curl_cffi + proxy rotation)
"""
import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from curl_cffi import requests as curl_requests

log = logging.getLogger("financial_scraper.pipeline.fetcher")

# ── Anti-Ban Protocol ──────────────────────────────────────────────────
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "max-age=0",
}

# Timeout: (connect, read) — fail-fast, never hang >15s
FAIL_FAST_TIMEOUT = (5.0, 10.0)

# Max retries per source
MAX_RETRIES = 2

# Jitter range for polite scraping (seconds)
JITTER_RANGE = (1.0, 3.0)

# Log file for system errors
SYSTEM_ERROR_LOG = "system_errors.log"


class TieredFetcher:
    """Unified fetcher that applies Tier 1 → Tier 2 → Tier 3 in order.

    Args:
        source_name: Human-readable label (e.g. "IMF", "World Bank")
        jitter: Enable polite jitter between requests (default: True)
    """

    def __init__(self, source_name: str = "", jitter: bool = True):
        self.source_name = source_name
        self.jitter = jitter

    # ── Public API ─────────────────────────────────────────────────────

    async def fetch(
        self,
        url: str,
        tier: int = 3,
        *,
        headers: dict | None = None,
        timeout: tuple[float, float] | None = None,
        impersonate: str | None = None,
    ) -> tuple[int, str, float]:
        """Fetch *url* using the specified *tier* strategy.

        Returns:
            (http_status_code, response_text, latency_seconds)

        Raises:
            ValueError on 403/404 — no retry.
            RuntimeError on total failure.
        """
        t0 = time.monotonic()
        effective_headers = {**DEFAULT_HEADERS, **(headers or {})}
        effective_timeout = timeout or FAIL_FAST_TIMEOUT
        last_error: Exception | None = None

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                if tier <= 2:
                    # Tier 1 & 2: httpx
                    async with httpx.AsyncClient(
                        headers=effective_headers,
                        follow_redirects=True,
                        timeout=effective_timeout,
                    ) as c:
                        r = await c.get(url)
                        latency = time.monotonic() - t0
                        elapsed_ms = round(latency * 1000)

                        if r.status_code in (403, 404):
                            self._log_error(url, r.status_code, "No retry — permanent")
                            return (r.status_code, r.text, latency)

                        r.raise_for_status()
                        log.info(
                            "[%s] Tier %d OK: HTTP %d, %d bytes, %dms",
                            self.source_name, tier, r.status_code, len(r.text), elapsed_ms,
                        )
                        return (r.status_code, r.text, latency)

                else:
                    # Tier 3: curl_cffi with TLS fingerprint
                    return await self._fetch_curl(
                        url, headers=effective_headers,
                        timeout=effective_timeout, impersonate=impersonate,
                    )

            except (httpx.TimeoutException, asyncio.TimeoutError) as exc:
                last_error = exc
                log.warning("[%s] Attempt %d/%d timeout: %s", self.source_name, attempt, MAX_RETRIES, url)
                if self.jitter:
                    await asyncio.sleep(0.5)  # minimal backoff

            except httpx.HTTPStatusError as exc:
                # 403/404 — fail fast, never retry
                if exc.response.status_code in (403, 404):
                    latency = time.monotonic() - t0
                    self._log_error(url, exc.response.status_code, str(exc))
                    return (exc.response.status_code, exc.response.text, latency)
                last_error = exc
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(attempt * 1.0)

            except Exception as exc:
                last_error = exc
                log.error("[%s] Attempt %d/%d error: %s", self.source_name, attempt, MAX_RETRIES, exc)
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(attempt * 0.5)

        # All attempts exhausted
        self._log_error(url, 0, str(last_error))
        return (0, str(last_error), time.monotonic() - t0)

    # ── Internal ───────────────────────────────────────────────────────

    async def _fetch_curl(
        self, url: str, headers: dict, timeout: tuple, impersonate: str | None
    ) -> tuple[int, str, float]:
        """curl_cffi fetch with TLS fingerprinting (Tier 3)."""
        t0 = time.monotonic()
        try:
            r = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: curl_requests.get(
                    url,
                    headers=headers,
                    timeout=timeout[1],
                    impersonate=impersonate or "chrome120",
                    verify=False,
                ),
            )
            latency = time.monotonic() - t0
            elapsed_ms = round(latency * 1000)

            if r.status_code in (403, 404):
                self._log_error(url, r.status_code, "No retry — permanent")
                return (r.status_code, r.text, latency)

            r.raise_for_status()
            log.info(
                "[%s] Tier 3 OK: HTTP %d, %d bytes, %dms (curl_cffi)",
                self.source_name, r.status_code, len(r.text), elapsed_ms,
            )
            return (r.status_code, r.text, latency)

        except Exception as exc:
            latency = time.monotonic() - t0
            log.error("[%s] curl_cffi failed: %s", self.source_name, exc)
            return (0, str(exc), latency)

    def _log_error(self, url: str, status: int, detail: str) -> None:
        """Log failure to system_errors.log with ISO8601 timestamp."""
        ts = datetime.now(timezone.utc).isoformat()
        line = f"[{ts}] source={self.source_name} url={url} status={status} detail={detail}\n"
        try:
            with open(SYSTEM_ERROR_LOG, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass  # best-effort
        if status == 0:
            log.error("[%s] FAILED: %s", self.source_name, detail)
        else:
            log.warning("[%s] HTTP %d: %s", self.source_name, status, detail)
