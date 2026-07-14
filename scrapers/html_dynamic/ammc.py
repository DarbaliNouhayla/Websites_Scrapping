import logging
import re
from datetime import datetime, timezone

from financial_scraper.scrapers.html_dynamic.base import BaseDynamicScraper
from financial_scraper.utils.text_utils import strip_html, truncate, is_boilerplate_title
from financial_scraper.utils.severity import get_severity

log = logging.getLogger("financial_scraper.scrapers.html_dynamic.ammc")

# AMMC article URLs end with a numeric ID: /fr/actualites/titre-de-larticle-1009
# Navigation/category links do NOT have this suffix → use it to filter junk
_ARTICLE_URL_PATTERN = re.compile(r"-\d+$")


def _is_article_url(url: str) -> bool:
    """Return True only for AMMC article URLs (have numeric suffix)."""
    path = url.rstrip("/").split("?")[0]
    return bool(_ARTICLE_URL_PATTERN.search(path))


class AMMCScraper(BaseDynamicScraper):
    slug = "ammc"
    base_url = "https://www.ammc.ma"
    listing_path = "/"
    wait_selector = "a[href*='actualites']"
    fetch_timeout_ms = 35_000

    async def scrape(self, fetcher) -> list[dict]:
        try:
            html = await fetcher.fetch(
                self.base_url,
                wait_selector=self.wait_selector,
                timeout_ms=self.fetch_timeout_ms,
                wait_until="domcontentloaded",
            )
        except Exception as exc:
            log.error("[ammc] Fetch failed: %s", exc)
            return []

        soup = self._soup(html)
        results = []
        seen_urls = set()

        for link in soup.select("a[href*='actualites']"):
            href = link.get("href", "")
            if not href:
                continue

            href = self._resolve_href(href, self.base_url)

            # Key fix: only process real article URLs (numeric ID suffix)
            # Filters out: /fr/actualites/communique-presse, /fr/actualites/ etc.
            if not _is_article_url(href):
                continue

            clean_href = self.clean_url_for_hash(href)
            if clean_href in seen_urls:
                continue
            seen_urls.add(clean_href)

            raw = strip_html(link.get_text())
            if is_boilerplate_title(raw):
                continue
            title = truncate(raw, 500)

            # Navigation labels are always short — real article titles are longer
            if not title or len(title) < 20:
                continue
            if self._skip_non_article(title, href):
                continue

            severity_result = await get_severity({
                "title": title,
                "description": "",
                "source": "ammc",
                "cve_score": None,
            })
            severity = severity_result["severity"]

            # Get listing description from parent container before fetching article
            listing_desc = ""
            parent = link.find_parent(["div", "li", "article"])
            if parent:
                desc_el = parent.select_one("p, .field-content, .teaser, .description")
                if desc_el:
                    listing_desc = truncate(strip_html(desc_el.get_text()), 2000)

            description, pub_date = await self._fetch_article_text(
                href, listing_description=listing_desc
            )
            pub_date = pub_date or datetime.now(timezone.utc)

            results.append(
                self._build_compliance_alert(
                    title=title,
                    description=description,
                    href=href,
                    regulator="AMMC",
                    pub_date=pub_date,
                    severity=severity,
                )
            )

        log.info("[ammc] scraped=%d items", len(results))
        return results