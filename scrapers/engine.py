import asyncio
import json
import logging
from datetime import datetime, timezone

from financial_scraper.fetchers.strategies import FetchStrategy
from financial_scraper.utils.dedup import UrlDeduplicator

log = logging.getLogger("financial_scraper.scrapers.engine")


class IntelligentEngine:
    def __init__(
        self,
        strategy: FetchStrategy,
        repository=None,
        dedup: UrlDeduplicator | None = None,
        rate_limit: float = 2.0,
        dev_mode: bool = True,
        resource_map: dict[str, str] | None = None,
    ):
        self._strategy = strategy
        self._repository = repository
        self._dedup = dedup or UrlDeduplicator()
        self._rate_limit = rate_limit
        self._dev_mode = dev_mode
        self._resource_map = resource_map or {}

    async def process(self, url: str, slug: str) -> dict | None:
        if self._dedup.is_seen(url):
            log.debug("[%s] Skipped (dedup) — %s", slug, url[:100])
            return None
        try:
            html = await self._strategy.fetch(url)
            self._dedup.mark_seen(url)
            return {"url": url, "slug": slug, "html": html}
        except Exception as exc:
            log.error("[%s] Fetch error: %s", slug, exc)
            return None

    async def process_batch(
        self, items: list[tuple[str, str]], concurrency: int = 3,
    ) -> list[dict]:
        sem = asyncio.Semaphore(concurrency)

        async def _fetch_one(url: str, slug: str) -> dict | None:
            async with sem:
                result = await self.process(url, slug)
                await asyncio.sleep(self._rate_limit)
                return result

        tasks = [_fetch_one(url, slug) for url, slug in items]
        results = await asyncio.gather(*tasks)
        return [r for r in results if r is not None]

    async def save_result(self, target_table: str, data: dict) -> bool:
        if self._dev_mode or self._repository is None:
            print(json.dumps({"_table": target_table, **data}, indent=2, default=str))
            return True
        try:
            return await self._repository.save(target_table, data)
        except Exception as exc:
            log.error("DB save error [%s]: %s", target_table, exc)
            return False

    async def update_health(self, slug: str, status: str, message: str = "", failures: int = 0) -> None:
        if self._repository and slug in self._resource_map:
            rid = self._resource_map[slug]
            await self._repository.update_health_status(rid, status, message, failures)

    async def close(self) -> None:
        await self._strategy.close()
