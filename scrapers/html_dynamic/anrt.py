import logging
from datetime import datetime, timezone

from financial_scraper.scrapers.html_dynamic.base import BaseDynamicScraper
from financial_scraper.utils.text_utils import strip_html, truncate, is_boilerplate_title, extract_title_from_url
from financial_scraper.utils.severity import get_severity

log = logging.getLogger("financial_scraper.scrapers.html_dynamic.anrt")

# ANRT injects CSRF session tokens into all links:
# /a-propos/communiques/title?csrt=2996028993524470078
# These tokens change every session — stripping them is critical
# for stable dedup hashing. clean_url_for_hash() in base handles this.


_ANRT_FALLBACK_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}

class ANRTScraper(BaseDynamicScraper):
    slug = "anrt"
    base_url = "https://www.anrt.ma"
    listing_path = "/"
    wait_selector = "main, section"
    fetch_timeout_ms = 35_000

    async def scrape(self, fetcher) -> list[dict]:
        html = await self._fetch_html(fetcher)
        if html is None:
            return []
        return await self._parse_html(html)

    async def _fetch_html(self, fetcher) -> str | None:
        url = f"{self.base_url}{self.listing_path}"
        try:
            html = await fetcher.fetch(
                url,
                wait_selector=self.wait_selector,
                timeout_ms=self.fetch_timeout_ms,
                wait_until="domcontentloaded",
            )
            if len(html) > 5000:
                log.info("[anrt] Playwright fetch OK: %d bytes", len(html))
                return html
            log.warning("[anrt] Playwright HTML too short (%d)", len(html))
        except Exception as exc:
            log.warning("[anrt] Playwright fetch failed: %s", exc)

        try:
            import httpx
            async with httpx.AsyncClient(headers=_ANRT_FALLBACK_HEADERS, follow_redirects=True, timeout=20.0) as c:
                r = await c.get(url)
                r.raise_for_status()
                log.info("[anrt] httpx fallback OK: %d bytes", len(r.text))
                return r.text
        except Exception as exc:
            log.error("[anrt] httpx fallback failed: %s", exc)
            return None

    async def _parse_html(self, html: str) -> list[dict]:
        soup = self._soup(html)
        results = []
        seen_urls = set()

        for link in soup.select("a[href*='communique']"):
            href = link.get("href", "")
            if not href:
                continue

            href = self._resolve_href(href, self.base_url)

            # CRITICAL: strip CSRT session token before dedup check
            clean_href = self.clean_url_for_hash(href)

            if clean_href in seen_urls:
                continue
            seen_urls.add(clean_href)

            raw = strip_html(link.get_text())
            if is_boilerplate_title(raw):
                # Try parent heading
                parent = link.find_parent(["div", "li", "article", "section"])
                if parent:
                    heading = parent.select_one("h1, h2, h3, h4, .title")
                    if heading:
                        raw = strip_html(heading.get_text())
            if is_boilerplate_title(raw):
                title = extract_title_from_url(clean_href)
            else:
                title = raw

            title = truncate(title, 500)
            if not title or len(title) < 10:
                continue
            if self._skip_non_article(title, href):
                continue

            severity_result = await get_severity({
                "title": title,
                "description": "",
                "source": "anrt",
                "cve_score": None,
            })
            severity = severity_result["severity"]

            listing_desc = ""
            parent = link.find_parent(["div", "li", "article", "section"])
            if parent:
                desc_el = parent.select_one("p, .field-content, .teaser")
                if desc_el:
                    listing_desc = truncate(strip_html(desc_el.get_text()), 2000)

            description, pub_date = await self._fetch_article_text(
                clean_href, listing_description=listing_desc
            )
            pub_date = pub_date or datetime.now(timezone.utc)

            results.append(
                self._build_compliance_alert(
                    title=title,
                    description=description,
                    href=clean_href,
                    regulator="ANRT",
                    pub_date=pub_date,
                    severity=severity,
                )
            )

        log.info("[anrt] scraped=%d items", len(results))
        return results