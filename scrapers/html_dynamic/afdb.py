import logging
import uuid
from datetime import datetime, timezone

import httpx

from financial_scraper.scrapers.html_dynamic.base import BaseDynamicScraper
from financial_scraper.utils.text_utils import strip_html, truncate, infer_trend, score_from_level
from financial_scraper.utils.severity import get_severity

log = logging.getLogger("financial_scraper.scrapers.html_dynamic.afdb")

_CLOUDFLARE_MARKERS = (
    "cf-browser-verification", "Checking your browser",
    "Vérification de sécurité", "Just a moment",
    "challenge-platform", "_cf_chl_opt",
)


class AfDBScraper(BaseDynamicScraper):
    slug = "afdb"
    base_url = "https://www.afdb.org"
    listing_path = "/fr/news-and-events"
    wait_selector = ".view-content, .views-row, article"
    fetch_timeout_ms = 30_000

    def _is_cloudflare(self, html: str) -> bool:
        return any(m in html for m in _CLOUDFLARE_MARKERS)

    async def _fallback_httpx(self, url: str) -> str | None:
        """Fallback: fetch with httpx + modern Chrome headers (bypasses simple CF)."""
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,fr;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Cache-Control": "max-age=0",
            "Sec-Ch-Ua": '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        }
        try:
            async with httpx.AsyncClient(
                headers=headers, follow_redirects=True, timeout=httpx.Timeout(25.0),
            ) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                return resp.text
        except Exception as exc:
            log.warning("[afdb] httpx fallback also failed: %s", exc)
            return None

    async def scrape(self, fetcher) -> list[dict]:
        html = None

        # Primary: Playwright with stealth
        try:
            html = await fetcher.fetch(
                f"{self.base_url}{self.listing_path}",
                wait_selector=self.wait_selector,
                timeout_ms=self.fetch_timeout_ms,
                wait_until="domcontentloaded",
            )
        except Exception as exc:
            log.warning("[afdb] Playwright fetch failed: %s", exc)

        # If Playwright returned Cloudflare challenge, try httpx fallback
        if html and self._is_cloudflare(html):
            log.warning("[afdb] Playwright got Cloudflare challenge — trying httpx fallback")
            html = await self._fallback_httpx(f"{self.base_url}{self.listing_path}")

        if not html:
            log.error("[afdb] All fetch methods failed — returning empty")
            return []

        if self._is_cloudflare(html):
            log.warning("[afdb] Still blocked by Cloudflare after fallback — html_len=%d", len(html))
            return []

        soup = self._soup(html)

        # AfDB uses Drupal views — try multiple card selectors
        cards = (
            soup.select(".view-content .views-row")
            or soup.select("article.node-news")
            or soup.select(".views-row")
            or soup.select("article")
        )

        results = []
        seen_urls = set()

        for card in cards:
            # TITLE — strict priority: headings first, NEVER img alt
            # Previous bug: a[href*='/news-and-events/'] was catching image wrapper links
            # whose text was the img alt attribute (person name/caption)
            title_el = (
                card.select_one("h3 a")
                or card.select_one("h4 a")
                or card.select_one("h2 a")
                or card.select_one(".views-field-title a")
                or card.select_one(".field-title a")
                # Only fall back to href-based selector as last resort
                # and only if it's NOT wrapping an image
            )

            # If no heading found, try href-based but validate it's not an image link
            if not title_el:
                for a in card.select("a[href*='/news-and-events/']"):
                    # Skip links that only contain an image (no real text)
                    text = a.get_text(strip=True)
                    if len(text) > 15 and not a.find("img"):
                        title_el = a
                        break

            if not title_el:
                continue

            href = title_el.get("href", "")
            if not href:
                continue

            href = self._resolve_href(href, self.base_url)

            if "news-and-events" not in href.lower():
                continue

            clean_href = self.clean_url_for_hash(href)
            if clean_href in seen_urls:
                continue
            seen_urls.add(clean_href)

            # Get title text — validate it's not an image caption
            title = truncate(strip_html(title_el.get_text()), 500)

            # Guard: if title looks like a person's name/caption (contains "directeur",
            # "président", "directrice" etc.) it's likely an img alt — skip
            if not title or len(title) < 10:
                continue
            caption_signals = ["directeur", "président", "directrice", "lors de", "lors des", "à brazzaville"]
            if any(sig in title.lower() for sig in caption_signals):
                continue

            # Summary
            summary_el = (
                card.select_one(".views-field-body .field-content")
                or card.select_one(".field-body")
                or card.select_one("p")
                or card.select_one(".teaser")
            )
            summary = truncate(strip_html(summary_el.get_text() if summary_el else ""), 2000)

            # Date — AfDB often shows "03-juil-2026" format in a span
            date_el = (
                card.select_one(".views-field-created .field-content")
                or card.select_one("time")
                or card.select_one(".date")
                or card.select_one("span[class*='date']")
            )
            pub_date = self._parse_date(date_el) or datetime.now(timezone.utc)

            # If summary is empty but description has date prefix ("03-juil-2026 Titre"),
            # strip the date from description
            if not summary and date_el:
                date_text = strip_html(date_el.get_text()).strip()
                if summary.startswith(date_text):
                    summary = summary[len(date_text):].strip()

            trend = infer_trend(title, summary)
            severity_result = await get_severity({
                "title": title,
                "description": summary,
                "source": "afdb",
                "cve_score": None,
            })
            severity = severity_result["severity"]

            results.append({
                "_target_table": "market",
                "id": str(uuid.uuid4()),
                "title": title,
                "description": summary or title,
                "category": "Developpement Afrique",
                "trend": trend,
                "change": None,
                "impact": severity,
                "impact_score": score_from_level(severity),
                "impact_detail": severity_result["reason"],
                "url": clean_href[:500],
                "source": "AfDB",
                "scraped_at": datetime.now(timezone.utc),
                "created_at": datetime.now(timezone.utc),
                "procurement_action": "monitor",
                "affected_suppliers": [],
            })

        log.info("[afdb] scraped=%d articles", len(results))
        return results