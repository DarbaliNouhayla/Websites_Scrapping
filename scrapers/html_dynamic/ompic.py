
import logging
import uuid
from datetime import datetime, timezone

import httpx

from financial_scraper.scrapers.html_dynamic.base import BaseDynamicScraper
from financial_scraper.utils.text_utils import strip_html, truncate

log = logging.getLogger("financial_scraper.scrapers.html_dynamic.ompic")

_OMPIC_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,*/*",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

_TENDER_URLS = [
    "https://www.directompic.ma/appels-d-offres",
    "https://www.directompic.ma/marches-publics",
    "https://www.directompic.ma/appel-offre",
    "https://www.ompic.ma/fr/appels-d-offres",
    "https://www.ompic.ma/fr/marches",
]


class OMPICScraper(BaseDynamicScraper):
    slug = "ompic"
    base_url = "https://www.directompic.ma"
    fetch_timeout_ms = 20_000

    async def scrape(self, fetcher) -> list[dict]:
        html = await self._fetch_tender_page()
        if not html:
            return []
        return self._parse_tenders(html)

    async def _fetch_tender_page(self) -> str | None:
        for url in _TENDER_URLS:
            try:
                async with httpx.AsyncClient(
                    headers=_OMPIC_HEADERS,
                    follow_redirects=True,
                    timeout=httpx.Timeout(20.0),
                ) as client:
                    resp = await client.get(url)
                    resp.raise_for_status()
                    log.info("[ompic] Loaded %d bytes from %s", len(resp.text), url)
                    return resp.text
            except Exception as exc:
                log.debug("[ompic] Failed %s: %s", url, exc)
                continue
        log.error("[ompic] All tender URLs failed \u2014 returning empty")
        return None

    def _parse_tenders(self, html: str) -> list[dict]:
        soup = self._soup(html)
        results = []
        seen_urls = set()
        now = datetime.now(timezone.utc)

        rows = (
            soup.select("article, .tender-item, .appel-offre, .marche-item, tr[class*='tender'], tr[class*='appel'], .list-group-item, .card")
            or soup.select("table tbody tr")
            or [soup]
        )

        for el in rows:
            link = el.select_one("a[href]") if el.name != "a" else el
            if not link:
                continue
            href = link.get("href", "")
            if not href:
                continue
            if not href.startswith("http"):
                href = self.base_url.rstrip("/") + ("/" if not href.startswith("/") else "") + href
            clean_href = self.clean_url_for_hash(href)
            if clean_href in seen_urls:
                continue
            seen_urls.add(clean_href)

            title_el = el.select_one("h2, h3, h4, .title, .objet") or link
            title = truncate(strip_html(title_el.get_text()), 500)
            if not title or len(title) < 5:
                continue

            desc_el = el.select_one("p, .description, .resume, .objet-marche")
            description = ""
            if desc_el:
                desc_text = strip_html(desc_el.get_text())
                if desc_text != title:
                    description = truncate(desc_text, 2000)

            buyer_el = el.select_one(".acheteur, .maitre-ouvrage, .organisme, [class*='buyer']")
            buyer = strip_html(buyer_el.get_text())[:255] if buyer_el else ""

            cat_el = el.select_one(".categorie, .type, [class*='category']")
            category = strip_html(cat_el.get_text())[:100] if cat_el else "Appel d'offres"

            deadline_el = el.select_one(".date-limite, .echeance, [class*='deadline'], time[datetime]")
            deadline = self._parse_date(deadline_el) if deadline_el else None

            date_el = el.select_one("time, .date, [class*='date'], .published")
            published = self._parse_date(date_el) if date_el else now

            results.append({
                "_target_table": "public_tender",
                "id": str(uuid.uuid4()),
                "title": title,
                "description": description or title,
                "buyer": buyer,
                "country": "Morocco",
                "scope": "national",
                "region": "",
                "category": category,
                "cpv_codes": [],
                "estimated_amount": None,
                "currency": "MAD",
                "procedure_type": "appel_offre_ouvert",
                "status": "open",
                "published_at": published,
                "deadline_at": deadline,
                "source": "OMPIC",
                "source_url": clean_href[:500],
                "resource_id": None,
                "it_relevance_score": 0,
                "created_at": now,
                "updated_at": now,
            })

        log.info("[ompic] scraped=%d tenders", len(results))
        return results
