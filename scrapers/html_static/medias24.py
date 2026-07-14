import logging
import uuid
from datetime import datetime, timezone

from financial_scraper.scrapers.html_static.base import BaseHTMLScraper
from financial_scraper.utils.text_utils import strip_html, truncate, infer_trend, score_from_level
from financial_scraper.utils.severity import get_severity

log = logging.getLogger("financial_scraper.scrapers.html_static.medias24")


class Medias24Scraper(BaseHTMLScraper):
    slug = "medias24"
    base_url = "https://medias24.com"
    listing_path = "/categorie/economie/"

    async def scrape(self, fetcher) -> list[dict]:
        try:
            html = await fetcher.fetch(f"{self.base_url}{self.listing_path}")
        except Exception as exc:
            log.error("[medias24] Listing page fetch failed: %s", exc)
            return []
        soup = self._soup(html)
        links = soup.select('a[href*="/202"]')
        seen = set()
        results = []

        for a_tag in links:
            href = a_tag.get("href", "")
            title_text = a_tag.get_text(strip=True)
            if not href or not title_text or len(title_text) < 20:
                continue
            if href.startswith("/"):
                href = self.base_url + href
            if not href.startswith(self.base_url):
                continue
            if href in seen:
                continue
            seen.add(href)

            if len(results) >= 10:
                break

            try:
                article_html = await fetcher.fetch(href)
                parsed = self._parse_article(article_html, href)
                if parsed is None:
                    continue
                # Classify severity via Groq
                severity_result = await get_severity({
                    "title": parsed["title"],
                    "description": parsed["description"],
                    "source": "medias24",
                    "cve_score": None,
                })
                parsed["impact"] = severity_result["severity"]
                parsed["impact_score"] = score_from_level(severity_result["severity"])
                parsed["impact_detail"] = severity_result["reason"]
                results.append(parsed)
            except Exception as exc:
                log.warning("[medias24] Failed to fetch %s: %s", href, exc)

        log.info("[medias24] scraped=%d articles", len(results))
        return results

    def _parse_article(self, html: str, url: str) -> dict | None:
        soup = self._soup(html)
        title_tag = soup.select_one("h1") or soup.select_one('[class*="BaskervilleSemiBold"].my-3')
        if not title_tag:
            return None
        title = truncate(strip_html(title_tag.get_text()), 500)

        desc_tag = soup.select_one("div.small-container.fs-3 p, div[class*=\"small-container\"] p") or soup.select_one("section.main-content-wrapper p")
        desc_text = strip_html(desc_tag.get_text()) if desc_tag else ""
        description = truncate(desc_text, 2000) if desc_text and desc_text != title else ""

        time_meta = soup.select_one("meta[property='article:published_time']")
        pub_date = None
        if time_meta and time_meta.get("content"):
            try:
                from dateutil import parser as dateparser
                pub_date = dateparser.parse(time_meta["content"]).replace(tzinfo=timezone.utc)
            except Exception:
                pass
        pub_date = pub_date or datetime.now(timezone.utc)

        section_meta = soup.select_one("meta[property='article:section']")
        category = section_meta.get("content", "Économie")[:50] if section_meta else "Économie"

        trend = infer_trend(title, description)

        return {
            "id": str(uuid.uuid4()),
            "title": title,
            "description": description,
            "category": category,
            "trend": trend,
            "change": None,
            "impact": "medium",
            "impact_score": 50,
            "impact_detail": "",
            "url": url[:500],
            "source": "Médias24",
            "scraped_at": datetime.now(timezone.utc),
            "created_at": datetime.now(timezone.utc),
            "procurement_action": "monitor",
            "affected_suppliers": [],
        }
