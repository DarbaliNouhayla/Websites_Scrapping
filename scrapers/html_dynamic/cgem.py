"""
cgem.py — CGEM scraper with httpx fallback.

CGEM URLs use cgem.ma (no www) while base_url has www.
Links are direct <a> tags, not wrapped in div.card containers.
"""
import logging
import uuid
from datetime import datetime, timezone

import httpx

from financial_scraper.scrapers.html_dynamic.base import BaseDynamicScraper
from financial_scraper.utils.text_utils import strip_html, truncate, infer_trend, score_from_level
from financial_scraper.utils.severity import get_severity

log = logging.getLogger("financial_scraper.scrapers.html_dynamic.cgem")

_CGEM_PATTERNS = ("cgem.ma/actualites/", "www.cgem.ma/actualites/")

_CGEM_FALLBACK_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,fr;q=0.8",
}


class CGEMScraper(BaseDynamicScraper):
    slug = "cgem"
    base_url = "https://www.cgem.ma"
    listing_path = "/actualites/"
    main_selector = ""
    fetch_timeout_ms = 35_000

    async def scrape(self, fetcher) -> list[dict]:
        html = await self._fetch_html(fetcher)
        if html is None:
            return []

        soup = self._soup(html)
        results = []
        seen_urls = set()

        # CGEM renders articles as direct <a> tags, not card containers
        for a_tag in soup.select("a[href*='/actualites/']"):
            href = a_tag.get("href", "")
            if not href:
                continue

            # Handle both absolute (https://cgem.ma/...) and relative (/actualites/...) URLs
            if href.startswith("/"):
                href = self.base_url + href

            # Verify it matches CGEM actualites
            if not any(p in href for p in _CGEM_PATTERNS):
                continue

            clean_href = self.clean_url_for_hash(href)
            if clean_href in seen_urls:
                continue
            seen_urls.add(clean_href)

            # CGEM <a> tags wrap images (no text) — try get_text, img alt, then parent heading
            raw = strip_html(a_tag.get_text())
            if not raw or len(raw) < 20:
                img = a_tag.select_one("img")
                if img and img.get("alt"):
                    raw = img["alt"].strip()
            if not raw or len(raw) < 20:
                parent = a_tag.find_parent(["div", "li", "article", "h2", "h3", "h4"])
                if parent:
                    heading = parent.select_one("h2, h3, h4, .title, strong")
                    if heading:
                        candidate = strip_html(heading.get_text())
                        if len(candidate) > len(raw):
                            raw = candidate
            title = truncate(raw, 500)
            if not title or len(title) < 20:
                continue
                continue

            # Description from parent container
            parent = a_tag.find_parent(["div", "li", "article"])
            desc_text = ""
            if parent:
                desc_el = parent.select_one("p, .description, .excerpt")
                if desc_el:
                    dt = strip_html(desc_el.get_text())
                    if dt and dt != title:
                        desc_text = truncate(dt, 2000)

            date_el = parent.select_one("time, .date, span[class*='date']") if parent else None
            pub_date = self._parse_date(date_el) or datetime.now(timezone.utc)

            severity_result = await get_severity({
                "title": title,
                "description": desc_text,
                "source": "cgem",
                "cve_score": None,
            })
            severity = severity_result["severity"]
            trend = infer_trend(title, desc_text)

            results.append({
                "_target_table": "market",
                "id": str(uuid.uuid4()),
                "title": title,
                "description": desc_text or title,
                "category": "Entreprises Maroc",
                "trend": trend,
                "change": None,
                "impact": severity,
                "impact_score": score_from_level(severity),
                "impact_detail": severity_result["reason"],
                "url": clean_href[:500],
                "source": "CGEM",
                "scraped_at": datetime.now(timezone.utc),
                "created_at": datetime.now(timezone.utc),
                "procurement_action": "monitor",
                "affected_suppliers": [],
            })

        log.info("[cgem] scraped=%d articles", len(results))
        return results

    async def _fetch_html(self, fetcher) -> str | None:
        """Fetch CGEM listing page. Tries Playwright first, falls back to httpx."""
        # Try Playwright
        try:
            html = await fetcher.fetch(
                f"{self.base_url}{self.listing_path}",
                timeout_ms=self.fetch_timeout_ms,
                wait_until="domcontentloaded",
            )
            if len(html) > 5000:
                log.info("[cgem] Playwright fetch OK: %d bytes", len(html))
                return html
            log.warning("[cgem] Playwright HTML too short (%d bytes) — trying httpx", len(html))
        except Exception as exc:
            log.warning("[cgem] Playwright fetch failed: %s — trying httpx", exc)

        # Fallback: httpx
        try:
            async with httpx.AsyncClient(
                headers=_CGEM_FALLBACK_HEADERS,
                follow_redirects=True,
                timeout=httpx.Timeout(20.0),
            ) as client:
                resp = await client.get(f"{self.base_url}{self.listing_path}")
                resp.raise_for_status()
                html = resp.text
                log.info("[cgem] httpx fallback OK: %d bytes", len(html))
                return html
        except Exception as exc:
            log.error("[cgem] httpx fallback also failed: %s", exc)
            return None
