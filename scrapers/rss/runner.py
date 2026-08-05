"""
runner.py — RSS pipeline with batch Groq severity classification.

For each feed:
  1. Parse feed via RSSParser
  2. Normalize all items
  3. Filter items needing Groq (not cached, not cyber)
  4. Batch-classify severity (10 items per Groq call → 10x faster)
  5. Save all items to DB
"""

import asyncio
import json
import logging
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

import os

from financial_scraper.scrapers.rss.feeds import ACTIVE_FEEDS
from financial_scraper.scrapers.rss.parser import RSSParser
from financial_scraper.scrapers.rss.normalizer import normalize
from financial_scraper.utils.cache import CheckpointStore
from financial_scraper.utils.dedup import UrlDeduplicator
from financial_scraper.utils.severity import classify_severity, classify_batch_inline
from financial_scraper.utils.text_utils import score_from_level

log = logging.getLogger("financial_scraper.scrapers.rss.runner")

_MAX_FEED_FAILURES = 3


def _apply_severity(out: dict, target_table: str, severity: str, reason: str) -> dict:
    """Map severity result to the correct fields per table type."""
    if target_table == "market":
        out["impact"] = severity
        out["impact_score"] = score_from_level(severity)
    elif target_table == "risks":
        out["level"] = severity
        out["score"] = score_from_level(severity)
    elif target_table == "alert":
        out["impact_level"] = severity
        out["priority_level"] = "high" if severity in ("critical", "high") else "medium"
    out["impact_detail"] = reason
    return out


class RSSPipeline:
    def __init__(
        self,
        repository=None,
        dedup: UrlDeduplicator | None = None,
        dev_mode: bool = True,
        checkpoint: CheckpointStore | None = None,
        resource_map: dict[str, str] | None = None,
    ):
        self._repository = repository
        self._dedup = dedup or UrlDeduplicator()
        self._dev_mode = dev_mode
        self._checkpoint = checkpoint or CheckpointStore()
        self._parser = RSSParser()
        self._executor = ThreadPoolExecutor(max_workers=4)
        self._resource_map = resource_map or {}

    async def run_unsafe(self) -> list:
        """Parse feeds, batch-classify severity, save to DB.

        Instead of one Groq call per item (slow), collects all items
        needing classification across all feeds and batch-classifies
        them 10 at a time via Groq. Saves ~90% of Groq wall-clock time.
        """
        summary: dict[str, int] = {"market": 0, "risks": 0, "alert": 0, "skipped": 0, "errors": 0}
        feeds = [f for f in ACTIVE_FEEDS if f.get("enabled", True)]
        log.info("[PIPELINE] Starting RSS pipeline — %d active feeds", len(feeds))

        feed_failures: dict[str, int] = defaultdict(int)

        for feed_config in feeds:
            slug = feed_config["slug"]

            if feed_failures.get(slug, 0) >= _MAX_FEED_FAILURES:
                log.warning("[%s] Skipped — %d consecutive failures", slug, feed_failures[slug])
                summary["skipped"] += 1
                continue

            try:
                items = await asyncio.get_event_loop().run_in_executor(
                    self._executor, self._parser.parse, feed_config
                )
            except Exception as exc:
                feed_failures[slug] += 1
                log.error("[%s] Parse error (%d/%d): %s", slug, feed_failures[slug], _MAX_FEED_FAILURES, exc)
                summary["errors"] += 1
                if self._repository and slug in self._resource_map:
                    await self._repository.update_health_status(
                        self._resource_map[slug], "degraded", str(exc)[:200], feed_failures[slug],
                    )
                continue

            feed_failures[slug] = 0
            if self._repository and slug in self._resource_map:
                await self._repository.update_health_status(
                    self._resource_map[slug], "healthy",
                )

            # NVD enrichment for CISA items (adds CVSS scores from NVD)
            if slug == "cisa" and os.getenv("USE_NVD_ENRICHMENT", "false").lower() == "true":
                cisa_without_score = sum(1 for i in items if i.extra.get("cve_id") and i.extra.get("cvss_score") is None)
                if cisa_without_score:
                    from financial_scraper.scrapers.api.nvd_enricher import enrich_cisa_items
                    log.info("[PIPELINE] Enriching %d CISA items with NVD CVSS scores...", cisa_without_score)
                    items = await enrich_cisa_items(items)
                    enriched = sum(1 for i in items if i.extra.get("cvss_score") is not None)
                    log.info("[PIPELINE] NVD enrichment: %d/%d items got CVSS score", enriched, cisa_without_score)

            # Phase 1: Normalize ALL items, collect batch-classification candidates
            all_normalized: list[tuple] = []  # (raw_item, normalized_dict)
            batch_candidates: list[dict] = []

            for raw_item in items:
                if not raw_item.url:
                    continue
                if self._dedup.is_seen(raw_item.url):
                    summary["skipped"] += 1
                    continue

                try:
                    normalized_list = normalize(raw_item)
                except Exception as exc:
                    log.error("[%s] Normalize error: %s", slug, exc)
                    summary["errors"] += 1
                    continue

                for normalized in normalized_list:
                    all_normalized.append((raw_item, normalized))

                    # Check if this item NEEDS Groq classification
                    if normalized.get("alert_type") == "cyber":
                        continue  # CVSS-based, no Groq
                    title = normalized.get("title", "")
                    if not title or len(title) < 3:
                        continue  # No title — defaulted to low later
                    if self._checkpoint.get(slug, title, normalized.get("description", "")):
                        continue  # Already cached

                    batch_candidates.append({
                        "source": slug,
                        "title": title,
                        "description": normalized.get("description", ""),
                    })

            # Phase 2: Batch-classify ALL uncached items across this feed
            if batch_candidates:
                log.info("[%s] Batch-classifying %d uncached items via Groq...", slug, len(batch_candidates))
                await classify_batch_inline(
                    batch_candidates,
                    batch_size=10,
                    cache=self._checkpoint,
                )

            # Phase 3: Process (classify + filter + save) each item
            seen_raw: set[int] = set()
            for raw_item, normalized in all_normalized:
                try:
                    await self._process_item(slug, raw_item, normalized, summary)
                except Exception as exc:
                    log.error("[%s] Item processing error: %s", slug, exc)
                    summary["errors"] += 1

                if id(raw_item) not in seen_raw:
                    seen_raw.add(id(raw_item))
                    self._dedup.mark_seen(raw_item.url)

            log.info("[%s] parsed=%d items", slug, len(items))

        dead_feeds = [slug for slug, count in feed_failures.items() if count >= _MAX_FEED_FAILURES]
        if dead_feeds:
            log.warning("[PIPELINE] Dead feeds skipped this run: %s", ", ".join(dead_feeds))
            if self._repository:
                for slug in dead_feeds:
                    rid = self._resource_map.get(slug)
                    if rid:
                        await self._repository.update_health_status(
                            rid, "down", f"{feed_failures[slug]} consecutive failures",
                            feed_failures[slug],
                        )

        log.info(
            "[PIPELINE] Phase 1+2 done. market=%d risks=%d alert=%d skipped=%d errors=%d",
            summary.get("market", 0), summary.get("risks", 0), summary.get("alert", 0),
            summary["skipped"], summary["errors"],
        )
        return []

    async def run(self) -> dict:
        """Full pipeline (kept for backward compatibility)."""
        await self.run_unsafe()
        return {"classified": 0}

    # ── helpers ──────────────────────────────────────────────────────────

    async def _process_item(
        self,
        slug: str,
        raw_item: "RawFeedItem",
        normalized: dict,
        summary: dict,
    ) -> None:
        target_table = normalized.pop("_target_table", "market")
        nd_url = normalized.pop("_url", "")
        normalized.pop("_needs_severity", None)

        # Cyber alerts (CISA) already have CVSS-based severity — save directly
        if normalized.get("alert_type") == "cyber":
            if self._dev_mode or self._repository is None:
                print(json.dumps({"_table": target_table, **normalized}, indent=2, default=str))
                summary[target_table] = summary.get(target_table, 0) + 1
            else:
                try:
                    saved = await self._repository.save(target_table, normalized)
                    if saved:
                        summary[target_table] = summary.get(target_table, 0) + 1
                    else:
                        summary["skipped"] += 1
                except Exception as exc:
                    log.error("[%s] DB save error: %s", slug, exc)
                    summary["errors"] += 1
            return

        title = normalized.get("title", "")
        description = normalized.get("description", "")

        # Skip Groq for empty/very short titles
        if not title or len(title) < 3:
            _apply_severity(normalized, target_table, "low", "No title — defaulted to low")
            await self._save_or_print(normalized, target_table, slug, summary)
            return

        # Check checkpoint — skip Groq if already classified
        cached = self._checkpoint.get(slug, title, description)
        if cached is not None:
            severity = cached["severity"]
            reason = cached.get("reason", "")
            _apply_severity(normalized, target_table, severity, reason)
            if self._dev_mode:
                out = {**normalized, "_table": target_table, "_checkpoint_skip": True}
                print(json.dumps(out, indent=2, default=str))
                summary[target_table] = summary.get(target_table, 0) + 1
            else:
                await self._save_or_print(normalized, target_table, slug, summary)
            return

        # Severity is already in checkpoint from batch classification.
        # This will never make a Groq call — it only reads cache.
        called_groq = False
        try:
            severity_result = await classify_severity(
                title=title,
                description=description,
                source=slug,
                cache=self._checkpoint,
            )
            severity = severity_result["severity"]
            reason = severity_result.get("reason", "")
            log.debug("[severity] %s → %s | %s", title[:50], severity, reason[:60] if reason else "")
        except Exception as exc:
            log.warning("[severity] Classification lookup failed for '%s': %s", title[:50], exc)
            severity = "medium"
            reason = "Classification unavailable"

        _apply_severity(normalized, target_table, severity, reason)

        # Filter: risk items below high are discarded
        if target_table == "risks" and severity not in ("high", "critical"):
            if self._dev_mode:
                out = {**normalized, "_table": target_table, "_filtered": "severity_too_low"}
                print(json.dumps(out, indent=2, default=str))
            summary["skipped"] += 1
            return

        # Filter: compliance alerts below critical are discarded
        if target_table == "alert" and severity != "critical" and normalized.get("alert_type") == "compliance":
            if self._dev_mode:
                out = {**normalized, "_table": target_table, "_filtered": "severity_too_low"}
                print(json.dumps(out, indent=2, default=str))
            summary["skipped"] += 1
            return

        await self._save_or_print(normalized, target_table, slug, summary)

    async def _save_or_print(self, data: dict, target_table: str, slug: str, summary: dict) -> None:
        if self._dev_mode or self._repository is None:
            print(json.dumps({"_table": target_table, **data}, indent=2, default=str))
            summary[target_table] = summary.get(target_table, 0) + 1
        else:
            try:
                saved = await self._repository.save(target_table, data)
                if saved:
                    summary[target_table] = summary.get(target_table, 0) + 1
                else:
                    summary["skipped"] += 1
            except Exception as exc:
                log.error("[%s] DB save error: %s", slug, exc)
                summary["errors"] += 1

    async def close(self) -> None:
        self._executor.shutdown(wait=False)
        self._checkpoint.close()
