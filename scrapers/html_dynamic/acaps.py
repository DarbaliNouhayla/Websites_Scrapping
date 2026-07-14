import logging
from datetime import datetime, timezone

from financial_scraper.scrapers.html_dynamic.base import BaseDynamicScraper
from financial_scraper.utils.text_utils import strip_html, truncate, is_boilerplate_title
from financial_scraper.utils.severity import get_severity

log = logging.getLogger("financial_scraper.scrapers.html_dynamic.acaps")


class ACAPSScraper(BaseDynamicScraper):
    slug = "acaps"
    base_url = "https://www.acaps.ma"
    listing_path = "/fr/actualites"
    wait_selector = "a[href*='actualite']"
    fetch_timeout_ms = 35_000

    async def scrape(self, fetcher) -> list[dict]:
        html = await self._fetch_html(fetcher)
        if html is None:
            return []
        return await self._parse_html(html)

    async def _fetch_html(self, fetcher) -> str | None:
        url = f"{self.base_url}{self.listing_path}"
        # Try Playwright
        try:
            html = await fetcher.fetch(
                url,
                wait_selector=self.wait_selector,
                timeout_ms=self.fetch_timeout_ms,
                wait_until="domcontentloaded",
            )
            if len(html) > 3000:
                log.info("[acaps] Playwright fetch OK: %d bytes", len(html))
                return html
            log.warning("[acaps] Playwright HTML too short (%d)", len(html))
        except Exception as exc:
            log.warning("[acaps] Playwright fetch failed: %s", exc)

        # httpx fallback
        try:
            import httpx
            h = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
                "Accept": "text/html,*/*",
                "Accept-Language": "fr-FR,fr;q=0.9",
            }
            async with httpx.AsyncClient(headers=h, follow_redirects=True, timeout=20.0) as c:
                r = await c.get(url)
                r.raise_for_status()
                log.info("[acaps] httpx fallback OK: %d bytes", len(r.text))
                return r.text
        except Exception as exc:
            log.error("[acaps] httpx fallback failed: %s", exc)
            return None

    async def _parse_html(self, html: str) -> list[dict]:
        soup = self._soup(html)
        results = []
        seen_urls = set()

        candidates = soup.select("a[href*='/actualites/'], a[href*='actualite']")

        for link in candidates:
            href = link.get("href", "")
            if not href:
                continue

            href = self._resolve_href(href, self.base_url)
            clean_href = self.clean_url_for_hash(href)

            if clean_href in seen_urls:
                continue
            seen_urls.add(clean_href)

            raw = strip_html(link.get_text())
            if is_boilerplate_title(raw):
                parent = link.find_parent(["div", "li", "article"])
                if parent:
                    heading = parent.select_one("h1, h2, h3, h4, .title")
                    if heading:
                        raw = strip_html(heading.get_text())
            if is_boilerplate_title(raw):
                continue
            title = truncate(raw, 500)
            if not title or len(title) < 10:
                continue
            if self._skip_non_article(title, href):
                continue

            severity_result = await get_severity({
                "title": title,
                "description": "",
                "source": "acaps",
                "cve_score": None,
            })
            severity = severity_result["severity"]

            listing_desc = ""
            if link.name != "a":
                desc_el = link.select_one("p, .field-content, .teaser, .description")
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
                    regulator="ACAPS",
                    pub_date=pub_date,
                    severity=severity,
                )
            )

        log.info("[acaps] scraped=%d items", len(results))
        return results