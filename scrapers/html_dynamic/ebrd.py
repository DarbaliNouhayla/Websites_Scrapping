import logging
import uuid
from datetime import datetime, timezone

from financial_scraper.scrapers.html_dynamic.base import BaseDynamicScraper
from financial_scraper.utils.text_utils import strip_html, truncate, infer_trend, score_from_level
from financial_scraper.utils.severity import get_severity

log = logging.getLogger("financial_scraper.scrapers.html_dynamic.ebrd")

# Geographic filter — EBRD covers 40+ countries, we only need
# content relevant to Morocco / North Africa / AWB's region
_GEO_KEYWORDS = [
    "morocco", "maroc", "marocaine", "marocain",
    "rabat", "casablanca", "marrakech", "tanger",
    "maghreb", "north africa", "afrique du nord",
    # Also keep broad financial/banking topics relevant to AWB
    "bank", "banking", "finance", "financial", "investment",
    "trade", "commerce", "infrastructure",
]


def _is_relevant(title: str, description: str) -> bool:
    combined = f"{title} {description}".lower()
    return any(kw in combined for kw in _GEO_KEYWORDS)


class EBRDScraper(BaseDynamicScraper):
    slug = "ebrd"
    base_url = "https://www.ebrd.com"
    listing_path = "/news"

    # EBRD uses JS to render news cards — must wait for them
    # Primary selector + fallbacks because EBRD changes class names periodically
    wait_selector = (
        "article.related-content__single-card-wrapper, "
        "article, .news-list, .related-content"
    )
    fetch_timeout_ms = 35_000

    async def scrape(self, fetcher) -> list[dict]:
        try:
            html = await fetcher.fetch(
                f"{self.base_url}{self.listing_path}",
                wait_selector=self.wait_selector,
                timeout_ms=self.fetch_timeout_ms,
                wait_until="domcontentloaded",
            )
        except Exception as exc:
            log.error("[ebrd] Fetch failed: %s", exc)
            return []

        soup = self._soup(html)

        # Fallback selector chain — EBRD changes CSS class names occasionally
        cards = (
            soup.select("article.related-content__single-card-wrapper")
            or soup.select("article[class*='card']")
            or soup.select("article")
            or soup.select(".news-list-item")
            or soup.select("[class*='news-card']")
            or soup.select("[class*='related-content']")
        )

        if not cards:
            log.warning("[ebrd] No cards found — HTML may not have rendered. html_len=%d", len(html))
            log.debug("[ebrd] First 500 chars: %s", html[:500].replace("\n", " "))

        results = []
        seen_urls = set()

        for card in cards:
            # Title: try known selectors then any heading
            title_el = (
                card.select_one(".related-content__title")
                or card.select_one("h2, h3, h4")
                or card.select_one("[class*='title']")
            )

            # Link: try known selectors then any internal link
            link_el = (
                card.select_one("a.related-content__btn-wrapper")
                or card.select_one("a[href*='/news/']")
                or card.select_one("a[href*='/content/']")
                or card.select_one("a[href]")
            )

            if not title_el or not link_el:
                continue

            href = link_el.get("href", "")
            if not href:
                continue

            href = self._resolve_href(href, self.base_url)

            if not href.startswith(self.base_url):
                continue

            clean_href = self.clean_url_for_hash(href)
            if clean_href in seen_urls:
                continue
            seen_urls.add(clean_href)

            # Description: try known selectors then any paragraph
            desc_el = (
                card.select_one(".related-content__text")
                or card.select_one("p:not([class*='label'])")
                or card.select_one("[class*='description']")
                or card.select_one("[class*='teaser']")
            )

            # Category label
            label_el = (
                card.select_one(".related-content__text-label")
                or card.select_one("[class*='label']")
                or card.select_one("[class*='tag']")
            )

            # Date
            date_el = (
                card.select_one(".related-content__date")
                or card.select_one("time")
                or card.select_one("[class*='date']")
            )

            title = truncate(strip_html(title_el.get_text()), 500)
            summary = truncate(strip_html(desc_el.get_text() if desc_el else ""), 2000)

            if not title:
                continue

            # Apply geographic/topic relevance filter
            if not _is_relevant(title, summary):
                continue

            pub_date = self._parse_date(date_el) or datetime.now(timezone.utc)
            trend = infer_trend(title, summary)
            severity_result = await get_severity({
                "title": title,
                "description": summary,
                "source": "ebrd",
                "cve_score": None,
            })
            severity = severity_result["severity"]
            category = strip_html(label_el.get_text()) if label_el else "Economie internationale"
            if not category or len(category) < 2:
                category = "Economie internationale"

            results.append({
                "_target_table": "market",
                "id": str(uuid.uuid4()),
                "title": title,
                "description": summary,
                "category": category[:50],
                "trend": trend,
                "change": None,
                "impact": severity,
                "impact_score": score_from_level(severity),
                "impact_detail": severity_result["reason"],
                "url": clean_href[:500],
                "source": "EBRD",
                "scraped_at": datetime.now(timezone.utc),
                "created_at": datetime.now(timezone.utc),
                "procurement_action": "monitor",
                "affected_suppliers": [],
            })

        log.info("[ebrd] scraped=%d articles", len(results))
        return results