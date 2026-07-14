import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Optional

from bs4 import BeautifulSoup

log = logging.getLogger("financial_scraper.scrapers.html_static.base")


class BaseHTMLScraper(ABC):
    slug: str = ""
    base_url: str = ""
    listing_path: str = ""

    @abstractmethod
    async def scrape(self, fetcher) -> list[dict]:
        ...

    def _soup(self, html: str) -> BeautifulSoup:
        return BeautifulSoup(html, "lxml")

    def _parse_date(self, tag) -> Optional[datetime]:
        if not tag:
            return None
        candidates = []
        if tag.name == "time" and tag.get("datetime"):
            candidates.append(tag["datetime"])
        if tag.get("content"):
            candidates.append(tag["content"])
        candidates.append(tag.get_text(strip=True))
        return self._try_parse_date(candidates)

    @staticmethod
    def _try_parse_date(candidates: list[str]) -> Optional[datetime]:
        from dateutil import parser as dateparser
        for c in candidates:
            if not c:
                continue
            try:
                return dateparser.parse(c).replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                continue
        return None
