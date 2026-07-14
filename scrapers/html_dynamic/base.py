import logging
import re
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

log = logging.getLogger("financial_scraper.scrapers.html_dynamic.base")

_STATIC_EXTENSIONS = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".zip", ".png", ".jpg", ".jpeg", ".gif"}
_NON_ARTICLE_PATTERNS = [
    "plan strategique", "plan stratégique", "politique qualite", "politique qualité",
    "rapport annuel", "rapport d'activite", "code de conduite", "charte",
    "organigramme", "calendrier", "formulaire",
]

# Default values for NOT NULL DB fields — never use None for these
DEFAULT_RECOMMENDED_ACTION = (
    "Surveiller l'évolution réglementaire et évaluer l'impact sur les activités."
)
DEFAULT_PROBABILITY_SCORE = 50


class BaseDynamicScraper(ABC):
    slug: str = ""
    base_url: str = ""
    listing_path: str = ""
    main_selector: str = ""
    wait_selector: str | None = None
    fetch_timeout_ms: int | None = None

    @abstractmethod
    async def scrape(self, fetcher) -> list[dict]:
        ...

    # ── HTML parsing ──────────────────────────────────────────────────

    def _soup(self, html: str) -> BeautifulSoup:
        return BeautifulSoup(html, "lxml")

    # ── URL utilities ─────────────────────────────────────────────────

    @staticmethod
    def clean_url_for_hash(url: str) -> str:
        """
        Strip query parameters and fragments before hashing.
        Prevents session tokens (e.g. ?csrt=...) from producing
        a different hash for the same article on every run.
        """
        parsed = urlparse(url)
        return parsed._replace(query="", fragment="").geturl()

    @staticmethod
    def _resolve_href(href: str, base_url: str) -> str:
        """Resolve relative hrefs to absolute URLs."""
        if href.startswith("http"):
            return href
        if href.startswith("/"):
            return base_url.rstrip("/") + href
        return href

    # ── Date parsing ──────────────────────────────────────────────────

    def _parse_date(self, tag) -> Optional[datetime]:
        if not tag:
            return None
        candidates = []
        if tag.name == "time" and tag.get("datetime"):
            candidates.append(tag["datetime"])
        if tag.get("content"):
            candidates.append(tag["content"])
        candidates.append(tag.get_text(strip=True))
        return self._try_parse_date(candidates)

    @staticmethod
    def _try_parse_date(candidates: list[str]) -> Optional[datetime]:
        from dateutil import parser as dateparser
        for c in candidates:
            if not c or len(c.strip()) < 4:
                continue
            try:
                return dateparser.parse(c, fuzzy=True).replace(tzinfo=timezone.utc)
            except (ValueError, TypeError, OverflowError):
                continue
        return None

    # ── URL filtering ─────────────────────────────────────────────────

    @staticmethod
    def _skip_non_article(title: str, url: str) -> bool:
        lower_url = url.lower()
        for ext in _STATIC_EXTENSIONS:
            if lower_url.endswith(ext):
                return True
        lower_title = title.lower()
        for pat in _NON_ARTICLE_PATTERNS:
            if pat in lower_title:
                return True
        return False

    # ── Article text fetching ─────────────────────────────────────────

    async def _fetch_article_text(
        self,
        url: str,
        listing_description: str = "",
    ) -> tuple[str, Optional[datetime]]:
        """
        Fetch article page with httpx to get description + publication date.

        Optimization: if listing_description is already meaningful (>80 chars),
        skip the HTTP fetch for description and only fetch if we need the date.
        Still fetches to get the date unless listing already provided it.

        Returns (description_text, publication_date).
        """
        pub_date = None

        # If listing already gave us a good description, avoid the extra HTTP call
        # for description — but still try to get the pub date
        skip_desc_fetch = len(listing_description.strip()) > 80

        if skip_desc_fetch:
            # Still need the date — lightweight fetch
            pass

        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=httpx.Timeout(10.0),
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
                    "Accept": "text/html,application/xhtml+xml,*/*",
                },
            ) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                html = resp.text
        except Exception as exc:
            log.debug("[%s] httpx fetch failed for %s: %s", self.slug, url, exc)
            return listing_description, pub_date

        soup = BeautifulSoup(html, "lxml")

        # ── Extract description ───────────────────────────────────────
        description = listing_description  # start with what the listing gave us

        if not skip_desc_fetch:
            # Try og:description first (usually most complete)
            og_desc = soup.select_one("meta[property='og:description']")
            if og_desc and og_desc.get("content"):
                candidate = og_desc["content"].strip()
                if len(candidate) > len(description):
                    description = candidate[:2000]

            # Fall back to meta description
            if not description:
                meta_tag = soup.select_one("meta[name='description']")
                if meta_tag and meta_tag.get("content"):
                    description = meta_tag["content"].strip()[:2000]

            # Fall back to first meaningful paragraph
            if not description:
                for tag in soup.select("article p, .content p, main p, p"):
                    text = tag.get_text(strip=True)
                    if len(text) > 50:
                        description = text[:2000]
                        break

        # ── Extract publication date ──────────────────────────────────
        for candidate_sel in (
            "meta[property='article:published_time']",
            "meta[property='og:updated_time']",
            "meta[name='date']",
            "time[datetime]",
            "[class*='date-display']",
            "[class*='published']",
            "[class*='date']",
        ):
            tag = soup.select_one(candidate_sel)
            if tag:
                d = self._parse_date(tag)
                if d:
                    pub_date = d
                    break

        return description[:2000], pub_date

    # ── Compliance alert builder ──────────────────────────────────────

    def _build_compliance_alert(
        self,
        *,
        title: str,
        description: str,
        href: str,
        regulator: str,
        pub_date: Optional[datetime] = None,
        severity: str = "low",
    ) -> dict:
        """
        Build a complete compliance alert dict with all NOT NULL fields populated.
        Use this in BAM, AMMC, ACAPS, ANRT, MEF scrapers to avoid repeating
        the same structure with None values.
        """
        from hashlib import sha256

        clean_href = self.clean_url_for_hash(href)
        source_id = sha256(clean_href.encode("utf-8")).hexdigest()[:64]
        now = datetime.now(timezone.utc)
        event_date = (pub_date or now).date()

        return {
            "title": title[:255],
            "description": description or DEFAULT_RECOMMENDED_ACTION,
            "alert_type": "compliance",
            "category": "Conformité réglementaire",
            "is_read": False,
            "created_at": now,
            "source_module": "scraping",
            "source_id": source_id,
            "source_url": href[:500],
            "supplier": "",
            "regulation": title[:120],
            "regulator": regulator,
            "event_date": event_date,
            "impact_level": severity,
            # ── NOT NULL fields — never None ──────────────────────────
            "recommended_action": DEFAULT_RECOMMENDED_ACTION,
            "probability_score": DEFAULT_PROBABILITY_SCORE,
            # ── Fixed fields for compliance alerts ────────────────────
            "workflow_status": "open",
            "incident_type": "regulatory_change",
            "priority_level": "high" if severity in ("critical", "high") else "medium",
            "continuity_impact": "unknown",
            # ── Cyber fields empty for compliance ─────────────────────
            "cve_id": "",
            "cve_url": "",
            "cve_score": None,
            "cyber_source": "",
            # ── Operational fields empty for compliance ───────────────
            "assigned_buyer": "",
            "status_page_url": "",
            "operational_source": "",
            "estimated_duration": "",
            "backup_supplier": "",
            "delivery_impact": "",
            # ── Signal source ─────────────────────────────────────────
            "signal_sources": [
                {
                    "source": regulator,
                    "url": href,
                    "scraped_at": now.isoformat(),
                }
            ],
        }