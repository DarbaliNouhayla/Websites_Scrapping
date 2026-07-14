import logging
import uuid
from datetime import datetime, timezone

from financial_scraper.scrapers.html_dynamic.base import BaseDynamicScraper
from financial_scraper.utils.text_utils import strip_html, truncate, infer_trend, score_from_level
from financial_scraper.utils.severity import get_severity

log = logging.getLogger("financial_scraper.scrapers.html_dynamic.worldbank")

_GEO_KEYWORDS = [
    "morocco", "maroc", "marocaine", "marocain",
    "rabat", "casablanca", "marrakech", "tanger",
    "maghreb",
]


def _is_morocco_related(title: str, description: str) -> bool:
    combined = f"{title} {description}".lower()
    return any(kw in combined for kw in _GEO_KEYWORDS)


class WorldBankScraper(BaseDynamicScraper):
    slug = "worldbank"
    base_url = "https://www.worldbank.org"
    listing_path = "/en/news/all"
    main_selector = "main div.search-listing-content"
    fetch_timeout_ms = 30_000

    async def scrape(self, fetcher) -> list[dict]:
        try:
            html = await fetcher.fetch(
                f"{self.base_url}{self.listing_path}",
                timeout_ms=self.fetch_timeout_ms,
                wait_until="domcontentloaded",
            )
        except Exception as exc:
            log.warning("[worldbank] Fetch failed: %s", exc)
            return []
        soup = self._soup(html)
        cards = soup.select("main div.search-listing-content")
        results = []

        for card in cards:
            title_el = card.select_one("h4 a")
            if not title_el:
                continue
            href = title_el.get("href", "")
            if not href:
                continue
            if href.startswith("/"):
                href = self.base_url + href
            if not href.startswith(self.base_url):
                continue

            desc_el = card.select_one("p.blurb-text span, p.blurb-text")
            date_el = card.select_one(".search-listing-info .info-list-item span:last-child, .search-listing-info span")

            title = truncate(strip_html(title_el.get_text()), 500)
            summary = truncate(strip_html(desc_el.get_text() if desc_el else ""), 2000)

            if not _is_morocco_related(title, summary):
                continue

            pub_date = self._parse_date(date_el) or datetime.now(timezone.utc)
            trend = infer_trend(title, summary)
            severity_result = await get_severity({
                "title": title,
                "description": summary,
                "source": "worldbank",
                "cve_score": None,
            })
            severity = severity_result["severity"]

            category = "Economie internationale"
            if "/press-release/" in href:
                category = "Communique de presse"
            elif "/feature/" in href:
                category = "Analyse"
            elif "/opinion/" in href:
                category = "Opinion"

            results.append({
                "_target_table": "market",
                "id": str(uuid.uuid4()),
                "title": title,
                "description": summary,
                "category": category,
                "trend": trend,
                "change": None,
                "impact": severity,
                "impact_score": score_from_level(severity),
                "impact_detail": severity_result["reason"],
                "url": href[:500],
                "source": "World Bank",
                "scraped_at": datetime.now(timezone.utc),
                "created_at": datetime.now(timezone.utc),
                "procurement_action": "monitor",
                "affected_suppliers": [],
            })

        log.info("[worldbank] scraped=%d articles", len(results))
        return results
