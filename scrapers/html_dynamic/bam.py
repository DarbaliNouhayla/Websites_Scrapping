"""
bam.py — Bank Al-Maghrib scraper using API interception.

BAM's communique list is loaded dynamically via an internal JSON API.
Playwright's page.content() returns the empty shell before the API responds.

Strategy: intercept XHR requests matching the BAM API endpoint pattern,
extract the JSON response body, and parse it directly — no DOM scraping needed.
"""

import json
import logging
import uuid
from datetime import datetime, timezone
from hashlib import sha256

from financial_scraper.scrapers.html_dynamic.base import BaseDynamicScraper
from financial_scraper.utils.text_utils import strip_html, truncate, is_boilerplate_title, extract_title_from_url
from financial_scraper.utils.severity import get_severity

log = logging.getLogger("financial_scraper.scrapers.html_dynamic.bam")

# BAM loads communiques via an API endpoint that returns JSON.
# Observed patterns:
#   /Communiques/GetCommuniques  or  /api/communiques
# The scraper intercepts ALL responses, filters by content-type=json,
# and checks for known BAM communique data structures.
_BAM_API_PATTERNS = (
    "communique",
    "getcommunique",
    "api/communique",
    "api/news",
    "api/actualite",
)


class BAMScraper(BaseDynamicScraper):
    slug = "bam"
    base_url = "https://www.bkam.ma"
    listing_path = "/Communiques"
    wait_selector = "a[href*='communique']"
    fetch_timeout_ms = 20_000

    async def scrape(self, fetcher) -> list[dict]:
        results = []
        seen_urls = set()
        captured_data: list[dict] = []

        # Use Playwright to intercept API responses
        browser = None
        try:
            # Access the internal Playwright browser
            browser = await fetcher._ensure_browser()
            context = await browser.new_context(
                locale="fr-FR",
                viewport={"width": 1280, "height": 720},
                timezone_id="Africa/Casablanca",
                ignore_https_errors=True,
            )
            page = await context.new_page()

            # Intercept network responses — capture JSON from BAM API endpoints
            async def on_response(response):
                url = response.url.lower()
                if not any(p in url for p in _BAM_API_PATTERNS):
                    return
                if "application/json" not in response.headers.get("content-type", ""):
                    return
                try:
                    body = await response.json()
                except Exception:
                    return

                # body may be a list (direct array) or dict with items key
                items = body if isinstance(body, list) else body.get("items", body.get("data", []))
                if items and isinstance(items, list):
                    captured_data.extend(items)
                    log.info("[bam] Intercepted %d items from %s", len(items), response.url)

            page.on("response", on_response)

            nav_timeout = self.fetch_timeout_ms
            await page.goto(
                f"{self.base_url}{self.listing_path}",
                wait_until="domcontentloaded",
                timeout=nav_timeout,
            )

            # Wait for API responses to come through
            await page.wait_for_timeout(5000)

            # Try to wait for the DOM selector too (in case JS renders links)
            try:
                await page.wait_for_selector(self.wait_selector, state="attached", timeout=8_000)
            except Exception:
                pass

            await context.close()
        except Exception as exc:
            log.warning("[bam] API interception failed: %s", exc)

        # If API interception captured items, parse them
        if captured_data:
            log.info("[bam] Processing %d API-captured items", len(captured_data))
            for item in captured_data:
                try:
                    record = self._parse_api_item(item, seen_urls)
                    if record:
                        results.append(record)
                except Exception as exc:
                    log.debug("[bam] Skipping API item: %s", exc)
                    continue

        # Fallback: if API interception returned nothing, try DOM then httpx
        if not results:
            if browser:
                log.warning("[bam] API interception returned 0 items — falling back to DOM scrape")
                try:
                    dom_results = await self._dom_scrape(fetcher)
                    results.extend(dom_results)
                except Exception as exc:
                    log.error("[bam] DOM fallback failed: %s", exc)
            if not results:
                log.warning("[bam] DOM returned 0 items — falling back to httpx")
                try:
                    httpx_results = await self._httpx_fallback()
                    results.extend(httpx_results)
                except Exception as exc:
                    log.error("[bam] httpx fallback failed: %s", exc)

        log.info("[bam] scraped=%d items", len(results))
        return results

    def _parse_api_item(self, item: dict, seen_urls: set) -> dict | None:
        """Parse a single item from the BAM API JSON response."""
        # Try multiple key possibilities for title, URL, date
        title = (
            item.get("title", "")
            or item.get("Titre", "")
            or item.get("libelle", "")
            or item.get("intitule", "")
        )
        href = (
            item.get("url", "")
            or item.get("Url", "")
            or item.get("lien", "")
            or item.get("link", "")
        )
        desc = (
            item.get("description", "")
            or item.get("Description", "")
            or item.get("resume", "")
            or item.get("contenu", "")
        )
        date_str = (
            item.get("date", "")
            or item.get("Date", "")
            or item.get("datePublication", "")
            or item.get("created_at", "")
        )

        if not title or not href:
            return None

        # Resolve relative URL
        href = self._resolve_href(href, self.base_url)
        clean_href = self.clean_url_for_hash(href)

        if clean_href in seen_urls:
            return None
        seen_urls.add(clean_href)

        # Clean title
        title = truncate(strip_html(title), 500)
        if is_boilerplate_title(title) or len(title) < 10:
            title = extract_title_from_url(href)
        if not title or len(title) < 10:
            return None
        if self._skip_non_article(title, href):
            return None

        # Parse date
        pub_date = None
        if date_str:
            try:
                from dateutil import parser as dateparser
                pub_date = dateparser.parse(date_str).replace(tzinfo=timezone.utc)
            except Exception:
                pass
        pub_date = pub_date or datetime.now(timezone.utc)

        title_truncated = truncate(title, 255)
        source_id = sha256(clean_href.encode("utf-8")).hexdigest()[:64]

        return {
            "title": title_truncated,
            "description": truncate(strip_html(desc), 2000) or title_truncated,
            "alert_type": "compliance",
            "category": "Conformite reglementaire",
            "is_read": False,
            "created_at": datetime.now(timezone.utc),
            "source_module": "scraping",
            "source_id": source_id,
            "source_url": clean_href[:500],
            "supplier": "",
            "regulation": title_truncated[:120],
            "regulator": "BAM",
            "event_date": pub_date.date(),
            "impact_level": "medium",
            "recommended_action": None,
            "workflow_status": "open",
            "incident_type": "regulatory_change",
            "cve_id": "",
            "cve_url": "",
            "cve_score": None,
            "cyber_source": "",
            "assigned_buyer": "",
            "priority_level": "medium",
            "continuity_impact": "unknown",
            "status_page_url": "",
            "operational_source": "",
            "estimated_duration": "",
            "backup_supplier": "",
            "delivery_impact": "",
            "probability_score": None,
            "signal_sources": [
                {
                    "source": "BAM",
                    "url": clean_href,
                    "scraped_at": datetime.now(timezone.utc).isoformat(),
                }
            ],
        }

    async def _dom_scrape(self, fetcher) -> list[dict]:
        """Fallback DOM-based scraping (old approach)."""
        try:
            html = await fetcher.fetch(
                f"{self.base_url}{self.listing_path}",
                wait_selector=self.wait_selector,
                timeout_ms=self.fetch_timeout_ms,
            )
        except Exception as exc:
            log.warning("[bam] DOM fallback fetch failed: %s", exc)
            return []

        soup = self._soup(html)
        return await self._parse_dom(soup)

    async def _httpx_fallback(self) -> list[dict]:
        """Pure httpx fallback — no Playwright needed."""
        h = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
        }
        try:
            import httpx
            async with httpx.AsyncClient(headers=h, follow_redirects=True, timeout=20.0) as c:
                r = await c.get(f"{self.base_url}{self.listing_path}")
                r.raise_for_status()
                log.info("[bam] httpx fallback OK: %d bytes", len(r.text))
        except Exception as exc:
            log.error("[bam] httpx fallback failed: %s", exc)
            return []
        soup = self._soup(r.text)
        return await self._parse_dom(soup)

    async def _parse_dom(self, soup) -> list[dict]:
        """Parse BAM communique links from a BeautifulSoup document."""
        results = []
        seen_urls = set()

        for link in soup.select("a[href*='Communique'], a[href*='communique'], a[href*='Communiques'], a[href*='ommunique']"):
            href = link.get("href", "")
            if not href:
                continue
            if href.startswith("/"):
                href = self.base_url + href
            clean_href = self.clean_url_for_hash(href)
            if clean_href in seen_urls:
                continue
            seen_urls.add(clean_href)

            # BAM link text is "Lire la suite" — try parent heading for real title
            raw = strip_html(link.get_text())
            if is_boilerplate_title(raw):
                parent = link.find_parent(["div", "li", "article"])
                if parent:
                    heading = parent.select_one("h2, h3, h4, .title, strong")
                    if heading:
                        raw = strip_html(heading.get_text())
            if is_boilerplate_title(raw):
                title = extract_title_from_url(clean_href)
            else:
                title = raw
            title = truncate(title, 500)
            if not title or len(title) < 10:
                continue
            if self._skip_non_article(title, clean_href):
                continue

            severity_result = await get_severity({
                "title": title,
                "description": "",
                "source": "bam",
                "cve_score": None,
            })
            severity = severity_result["severity"]
            source_id = sha256(clean_href.encode("utf-8")).hexdigest()[:64]

            description, pub_date = await self._fetch_article_text(clean_href)
            pub_date = pub_date or datetime.now(timezone.utc)

            results.append({
                "title": truncate(title, 255),
                "description": description,
                "alert_type": "compliance",
                "category": "Conformite reglementaire",
                "is_read": False,
                "created_at": datetime.now(timezone.utc),
                "source_module": "scraping",
                "source_id": source_id,
                "source_url": clean_href[:500],
                "supplier": "",
                "regulation": truncate(title, 120),
                "regulator": "BAM",
                "event_date": pub_date.date(),
                "impact_level": severity,
                "recommended_action": None,
                "workflow_status": "open",
                "incident_type": "regulatory_change",
                "cve_id": "",
                "cve_url": "",
                "cve_score": None,
                "cyber_source": "",
                "assigned_buyer": "",
                "priority_level": "medium",
                "continuity_impact": "unknown",
                "status_page_url": "",
                "operational_source": "",
                "estimated_duration": "",
                "backup_supplier": "",
                "delivery_impact": "",
                "probability_score": None,
                "signal_sources": [
                    {
                        "source": "BAM",
                        "url": clean_href,
                        "scraped_at": datetime.now(timezone.utc).isoformat(),
                    }
                ],
            })

        return results
