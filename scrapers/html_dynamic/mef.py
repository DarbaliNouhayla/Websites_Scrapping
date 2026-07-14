import logging
from datetime import datetime, timezone

from financial_scraper.scrapers.html_dynamic.base import BaseDynamicScraper
from financial_scraper.utils.text_utils import strip_html, truncate, is_boilerplate_title, infer_trend
from financial_scraper.utils.severity import get_severity

log = logging.getLogger("financial_scraper.scrapers.html_dynamic.mef")

# finances.gov.ma uses a custom CMS — links do NOT use /communique/ or /actualite/
# Actual URL patterns observed:
#   /fr/content/titre-de-larticle
#   /fr/document/rapport-2026
#   /fr/article/...
#   /fr/publication/...
#   /fr/press/...
# The wait_selector was matching nothing because the selector
# "a[href*='communique']" never appears on this site.


_MEF_FALLBACK_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}

class MEFScraper(BaseDynamicScraper):
    slug = "mef"
    base_url = "https://www.finances.gov.ma"
    listing_path = "/fr/Pages/index.aspx"

    wait_selector = "main, #main-content, .view-content, article, #block-system-main"

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
                timeout_ms=20_000,
            )
            if len(html) > 5000:
                log.info("[mef] Playwright fetch OK: %d bytes", len(html))
                return html
            log.warning("[mef] Playwright HTML too short (%d)", len(html))
        except Exception as exc:
            log.warning("[mef] Playwright fetch failed: %s", exc)

        try:
            import httpx
            async with httpx.AsyncClient(headers=_MEF_FALLBACK_HEADERS, follow_redirects=True, timeout=20.0) as c:
                r = await c.get(url)
                r.raise_for_status()
                log.info("[mef] httpx fallback OK: %d bytes", len(r.text))
                return r.text
        except Exception as exc:
            log.error("[mef] httpx fallback failed: %s", exc)
            return None

    async def _parse_html(self, html: str) -> list[dict]:
        soup = self._soup(html)
        results = []
        seen_urls = set()

        all_links = soup.select("a[href]")
        mef_patterns = (
            "/fr/content/",
            "/fr/article/",
            "/fr/document/",
            "/fr/publication/",
            "/fr/press/",
            "/fr/actualites/",
            "/fr/communiques/",
            "/fr/presse/",
            "actualite",
            "communique",
            "publication",
            "presse",
        )

        for link in all_links:
            href = link.get("href", "")
            if not href:
                continue

            href = self._resolve_href(href, self.base_url)

            if self.base_url not in href:
                continue
            if not any(pat in href.lower() for pat in mef_patterns):
                continue

            clean_href = self.clean_url_for_hash(href)
            if clean_href in seen_urls:
                continue
            seen_urls.add(clean_href)

            if self._skip_non_article("", href):
                continue

            raw = strip_html(link.get_text())
            if is_boilerplate_title(raw) or len(raw) < 10:
                parent = link.find_parent(["div", "li", "article", "td"])
                if parent:
                    heading = parent.select_one("h2, h3, h4, .title, strong")
                    if heading:
                        raw = strip_html(heading.get_text())

            title = truncate(raw, 500)
            if not title or len(title) < 15:
                continue
            if self._skip_non_article(title, href):
                continue

            severity_result = await get_severity({
                "title": title,
                "description": "",
                "source": "mef",
                "cve_score": None,
            })
            severity = severity_result["severity"]

            listing_desc = ""
            parent = link.find_parent(["div", "li", "article"])
            if parent:
                desc_el = parent.select_one("p, .field-content, .teaser")
                if desc_el:
                    candidate = strip_html(desc_el.get_text())
                    if candidate != title:
                        listing_desc = truncate(candidate, 2000)

            description, pub_date = await self._fetch_article_text(
                clean_href, listing_description=listing_desc
            )
            pub_date = pub_date or datetime.now(timezone.utc)

            results.append(
                self._build_compliance_alert(
                    title=title,
                    description=description,
                    href=clean_href,
                    regulator="MEF",
                    pub_date=pub_date,
                    severity=severity,
                )
            )

        log.info("[mef] scraped=%d items", len(results))
        return results