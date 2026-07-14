import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from io import BytesIO

import feedparser
import httpx
from lxml import etree

log = logging.getLogger("financial_scraper.scrapers.rss.parser")

# Default minimalist headers
FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Full modern Chrome fingerprint for blocked feeds (403 Forbidden fix)
# Includes Sec-* headers that legitimate browsers always send
HEADLESS_CHROME_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
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

# Feeds that need the full Chrome header set to avoid 403 blocks
HEADER_PROFILES: dict[str, dict] = {
    "industries_maroc": HEADLESS_CHROME_HEADERS,
    "imf_news": HEADLESS_CHROME_HEADERS,
    "oecd": HEADLESS_CHROME_HEADERS,
    "afdb_news": HEADLESS_CHROME_HEADERS,
}


@dataclass
class RawFeedItem:
    title: str = ""
    description: str = ""
    url: str = ""
    category: str = "general"
    published: datetime | None = None
    source_name: str = ""
    source_slug: str = ""
    language: str = "en"
    extra: dict = field(default_factory=dict)


class RSSParser:
    def parse(self, feed_config: dict) -> list[RawFeedItem]:
        slug = feed_config["slug"]
        url = feed_config["url"]

        # Handle JSON feeds (like CISA KEV)
        if feed_config.get("feed_type") == "json":
            return self._parse_json(feed_config)

        # Handle IMF news scrape (Next.js RSS returns HTML shell, need to extract articles)
        if feed_config.get("feed_type") == "imf_scrape":
            return self._parse_imf_scrape(feed_config)

        # Handle Industries.ma scrape (RSS feed 403 even with curl_cffi)
        if feed_config.get("feed_type") == "industries_scrape":
            return self._parse_industries_scrape(feed_config)

        # Handle sitemap-based feeds (e.g. ENISA has no RSS but has a working sitemap)
        if feed_config.get("feed_type") == "sitemap":
            return self._parse_sitemap(feed_config)

        log.info("[%s] Parsing feed: %s", slug, url)

        # Use feed-specific headers if the slug has a custom header profile
        feed_headers = HEADER_PROFILES.get(slug, None)
        needs_curl = feed_config.get("needs_curl", False)
        raw_xml = self._fetch_raw(url, headers=feed_headers, needs_curl=needs_curl)
        clean_xml = self._recover_xml(raw_xml)
        parsed = feedparser.parse(BytesIO(clean_xml))

        if parsed.bozo and parsed.bozo_exception:
            log.warning("[%s] Feed parse warning (recovered): %s", slug, parsed.bozo_exception)

        items: list[RawFeedItem] = []
        for entry in parsed.entries:
            desc = entry.get("summary") or entry.get("description") or ""
            if not desc:
                desc = self._get_content_encoded(entry)

            item = RawFeedItem(
                title=entry.get("title", "") or "",
                description=desc,
                url=entry.get("link", "") or "",
                category=self._extract_category(entry),
                published=self._extract_date(entry),
                source_name=feed_config.get("slug", slug),
                source_slug=slug,
                language=feed_config.get("language", "en"),
                extra={
                    "target_table": feed_config.get("target_table", "market"),
                    "alert_type": feed_config.get("alert_type", ""),
                    "cyber_source": feed_config.get("cyber_source", ""),
                    "also_risks": feed_config.get("also_risks", False),
                },
            )
            items.append(item)

        log.info("[%s] parsed=%d items", slug, len(items))
        return items

    def _parse_imf_scrape(self, feed_config: dict) -> list[RawFeedItem]:
        """Scrape IMF news page — Next.js renders articles dynamically."""
        slug = feed_config["slug"]
        url = feed_config["url"]
        log.info("[%s] Scraping IMF news: %s", slug, url)
        from bs4 import BeautifulSoup
        raw = self._fetch_raw(url, needs_curl=True)
        soup = BeautifulSoup(raw, "lxml")
        items: list[RawFeedItem] = []
        seen = set()
        for a in soup.select("a[href*='/en/news/articles/']"):
            href = a.get("href", "")
            if not href:
                continue
            if not href.startswith("http"):
                href = "https://www.imf.org" + href
            if href in seen:
                continue
            seen.add(href)
            text = a.get_text(strip=True)
            if not text or len(text) < 30:
                continue
            items.append(RawFeedItem(
                title=text,
                description=text,
                url=href,
                source_name=feed_config.get("source_name", "IMF"),
                source_slug=slug,
                language=feed_config.get("language", "en"),
                extra={
                    "target_table": feed_config.get("target_table", "market"),
                    "also_risks": feed_config.get("also_risks", False),
                },
            ))
        log.info("[%s] scraped=%d items", slug, len(items))
        return items

    def _parse_industries_scrape(self, feed_config: dict) -> list[RawFeedItem]:
        """Scrape Industries.ma home page for article links (RSS feed is 403)."""
        slug = feed_config["slug"]
        url = feed_config["url"]
        log.info("[%s] Scraping Industries.ma: %s", slug, url)
        from bs4 import BeautifulSoup
        raw = self._fetch_raw(url, headers=FETCH_HEADERS)
        soup = BeautifulSoup(raw, "lxml")
        for t in soup(["script", "style", "noscript"]):
            t.decompose()
        items: list[RawFeedItem] = []
        seen = set()
        # industries.ma article URLs are full domain + slug:
        # <a href="https://industries.ma/bank-of-africa-renforce...">
        # Filter for article-like slugs (single path segment, no category prefix)
        skip_prefixes = ("/category/", "/page/", "/tag/", "/author/", "/wp-", "/youtube-idm",
                         "/abonnement", "/qui-sommes", "/publicite", "/conditions-", "/politique-de",
                         "/jobs", "/leadership")
        for a in soup.select("a[href^='https://industries.ma/']"):
            href = a.get("href", "").strip().rstrip("/")
            if not href:
                continue
            path = href.replace("https://industries.ma/", "")
            # Skip category/subdirectory links and nav pages
            if "/" in path:
                continue
            if any(s in href for s in skip_prefixes):
                continue
            # Must be a reasonably long kebab-case slug
            if len(path) < 20 or "-" not in path:
                continue
            text = a.get_text(strip=True)
            if not text or len(text) < 25:
                continue
            if href in seen:
                continue
            seen.add(href)
            items.append(RawFeedItem(
                title=text,
                description=text,
                url=href,
                source_name=feed_config.get("source_name", "Industrie du Maroc"),
                source_slug=slug,
                language=feed_config.get("language", "fr"),
                extra={
                    "target_table": feed_config.get("target_table", "market"),
                    "also_risks": feed_config.get("also_risks", False),
                },
            ))
        log.info("[%s] scraped=%d items", slug, len(items))
        return items

    def _parse_sitemap(self, feed_config: dict) -> list[RawFeedItem]:
        slug = feed_config["slug"]
        url = feed_config["url"]
        log.info("[%s] Parsing sitemap: %s", slug, url)
        raw = self._fetch_raw(url, needs_curl=feed_config.get("needs_curl", False))
        import xml.etree.ElementTree as ET
        root = ET.fromstring(raw)
        ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        items: list[RawFeedItem] = []
        filter_path = feed_config.get("sitemap_filter", "/news/")
        for url_el in root.findall(".//sm:url", ns):
            loc = url_el.findtext("sm:loc", "", ns)
            if not loc or filter_path not in loc:
                continue
            lastmod = url_el.findtext("sm:lastmod", "", ns)
            pub_date = None
            if lastmod:
                try:
                    pub_date = datetime.fromisoformat(lastmod.replace("Z", "+00:00"))
                except Exception:
                    pass
            path = loc.rstrip("/").rsplit("/", 1)[-1]
            title = path.replace("-", " ").replace("_", " ").title()
            items.append(RawFeedItem(
                title=title[:200] if title else loc,
                description=loc,
                url=loc,
                published=pub_date,
                source_name=feed_config.get("source_name", slug),
                source_slug=slug,
                language=feed_config.get("language", "en"),
                extra={
                    "target_table": feed_config.get("target_table", "market"),
                    "alert_type": feed_config.get("alert_type", ""),
                    "cyber_source": feed_config.get("cyber_source", ""),
                    "also_risks": feed_config.get("also_risks", False),
                },
            ))
        items.sort(key=lambda i: i.published or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        max_items = feed_config.get("max_items", 50)
        items = items[:max_items]
        log.info("[%s] sitemap: %d items (from %d /news URLs)", slug, len(items), sum(1 for u in root.findall(".//sm:url", ns) if filter_path in (u.findtext("sm:loc", "", ns) or "")))
        return items

    def _fetch_raw(self, url: str, headers: dict | None = None, needs_curl: bool = False) -> bytes:
        request_headers = headers if headers is not None else FETCH_HEADERS
        try:
            resp = httpx.get(url, headers=request_headers, follow_redirects=True, timeout=10.0)
            resp.raise_for_status()
            return resp.content
        except Exception as exc:
            if not needs_curl:
                raise
            # Fallback to curl_cffi for Cloudflare/WAF blocked feeds
            log.warning("httpx failed, trying curl_cffi fallback: %s", exc)
            return self._fetch_via_curl(url, headers=request_headers)

    def _fetch_via_curl(self, url: str, headers: dict | None = None) -> bytes:
        try:
            from curl_cffi import requests as curl_req
            resp = curl_req.get(
                url,
                headers=headers or FETCH_HEADERS,
                timeout=15.0,
                impersonate="chrome120",
                verify=False,
            )
            resp.raise_for_status()
            return resp.content
        except Exception as exc:
            raise RuntimeError(f"curl_cffi also failed: {exc}") from exc

    def _recover_xml(self, raw: bytes) -> bytes:
        try:
            parser = etree.XMLParser(recover=True, no_network=True)
            tree = etree.fromstring(raw, parser)
            return etree.tostring(tree, xml_declaration=True, encoding="utf-8")
        except Exception:
            return raw

    def _get_content_encoded(self, entry) -> str:
        for key in ("content_encoded", "content:encoded", "content"):
            val = getattr(entry, key, None) or entry.get(key, "")
            if val:
                if isinstance(val, list) and len(val) > 0:
                    val = val[0].get("value", "") if isinstance(val[0], dict) else str(val[0])
                return val
        return ""

    def _extract_category(self, entry) -> str:
        cats = []
        if hasattr(entry, "tags") and entry.tags:
            for tag in entry.tags:
                term = tag.get("term", "") or tag.get("label", "") or ""
                if term:
                    cats.append(term)
        if not cats:
            cat = entry.get("category", "")
            if cat:
                cats = [cat]
        if cats:
            return cats[0][:50]
        return "general"

    def _extract_date(self, entry) -> datetime | None:
        import time as time_mod
        dt = None
        for attr in ("published_parsed", "updated_parsed"):
            tp = getattr(entry, attr, None)
            if tp:
                try:
                    dt = datetime.fromtimestamp(time_mod.mktime(tp), tz=timezone.utc)
                except Exception:
                    pass
                break
        return dt

    def _parse_json(self, feed_config: dict) -> list[RawFeedItem]:
        slug = feed_config["slug"]
        url = feed_config["url"]
        log.info("[%s] Parsing JSON feed: %s", slug, url)

        feed_headers = HEADER_PROFILES.get(slug, FETCH_HEADERS)
        resp = httpx.get(url, headers=feed_headers, follow_redirects=True, timeout=15.0)
        resp.raise_for_status()
        data = resp.json()

        items: list[RawFeedItem] = []
        vulns = data.get("vulnerabilities", [])
        for vuln in vulns:
            cve_id = vuln.get("cveID", "")
            title = vuln.get("vulnerabilityName", "") or cve_id
            desc = vuln.get("shortDescription", "") or ""
            date_str = vuln.get("dateAdded", "")
            pub = None
            if date_str:
                try:
                    pub = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                except ValueError:
                    pass

            cvss = vuln.get("cvssScore")
            cvss_float = float(cvss) if cvss is not None else None

            item = RawFeedItem(
                title=title,
                description=desc,
                url=f"https://www.cisa.gov/known-exploited-vulnerabilities/{cve_id.lower()}",
                category="Cybersécurité",
                published=pub,
                source_name=slug,
                source_slug=slug,
                language="en",
                extra={
                    "target_table": "alert",
                    "alert_type": "cyber",
                    "cyber_source": "CISA",
                    "cve_id": cve_id,
                    "cvss_score": cvss_float,
                    "also_risks": False,
                },
            )
            items.append(item)

        max_items = feed_config.get("max_items", 50)
        items = items[:max_items]
        log.info("[%s] parsed=%d items from JSON (capped at %d)", slug, len(items), max_items)
        return items
