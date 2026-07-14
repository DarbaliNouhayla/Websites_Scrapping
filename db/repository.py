"""
repository.py — PostgreSQL persistence layer for the scraping pipeline.

Uses asyncpg connection pool.
Handles 4 target tables: market, risks, alert, public_tender.
Also manages scraping_resource registry (upsert on startup).

DEDUPLICATION STRATEGY (2 layers):
  Layer 1 — In-memory: UrlDeduplicator in engine/runner (already implemented)
  Layer 2 — Database: Check existence before INSERT (implemented here)
    market:        WHERE url = $1
    risks:         WHERE url = $1
    alert:         WHERE source_id = $1
    public_tender: WHERE source_url = $1
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

import asyncpg

log = logging.getLogger(__name__)


class PostgreSQLRepository:
    """
    Async PostgreSQL repository.
    One instance shared across the entire pipeline session.
    All methods are safe to call concurrently.
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: Optional[asyncpg.Pool] = None

    # ── Connection management ─────────────────────────────────────────

    async def connect(self) -> None:
        """Create the connection pool. Call once at startup."""
        self._pool = await asyncpg.create_pool(
            self._dsn,
            min_size=2,
            max_size=10,
            command_timeout=30,
        )
        log.info("[db] PostgreSQL pool connected: %s", self._dsn.split("@")[-1])

    async def close(self) -> None:
        """Close the pool. Call at shutdown."""
        if self._pool:
            await self._pool.close()
            log.info("[db] PostgreSQL pool closed")

    # ── Deduplication checks ──────────────────────────────────────────

    async def _exists_market(self, url: str) -> bool:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id FROM market WHERE url = $1 LIMIT 1", url
            )
            return row is not None

    async def _exists_risks(self, url: str) -> bool:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id FROM risks WHERE url = $1 LIMIT 1", url
            )
            return row is not None

    async def _exists_alert(self, source_id: str) -> bool:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id FROM alert WHERE source_id = $1 LIMIT 1", source_id
            )
            return row is not None

    async def _exists_tender(self, source_url: str) -> bool:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id FROM public_tender WHERE source_url = $1 LIMIT 1",
                source_url
            )
            return row is not None

    # ── market table ──────────────────────────────────────────────────

    async def save_market(self, data: dict) -> bool:
        """
        Insert into market table.
        Returns True if inserted, False if duplicate skipped.
        """
        url = data.get("url") or ""
        if url and await self._exists_market(url):
            log.debug("[db] market duplicate skipped: %s", url[:80])
            return False

        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO market (
                    id, title, description, category, trend, change,
                    impact, impact_score, impact_detail, url, source,
                    scraped_at, created_at, procurement_action,
                    affected_suppliers, resource_id
                ) VALUES (
                    $1, $2, $3, $4, $5, $6,
                    $7, $8, $9, $10, $11,
                    $12, $13, $14,
                    $15::jsonb, $16
                )
                ON CONFLICT (id) DO NOTHING
                """,
                str(data.get("id", uuid.uuid4())),
                str(data.get("title", ""))[:500],
                str(data.get("description", "")),
                str(data.get("category", "general"))[:50],
                str(data.get("trend", "stable"))[:20],
                float(data.get("change") or 0.0),
                str(data.get("impact", "medium"))[:20],
                int(data.get("impact_score") or 50),
                str(data.get("impact_detail", "")),
                str(url)[:2000] if url else None,
                str(data.get("source", ""))[:100] if data.get("source") else None,
                data.get("scraped_at") or datetime.now(timezone.utc),
                data.get("created_at") or datetime.now(timezone.utc),
                str(data.get("procurement_action", "monitor"))[:80],
                json.dumps(data.get("affected_suppliers") or []),
                str(data.get("resource_id"))[:36] if data.get("resource_id") else None,
            )

        log.info("[db] market saved: %s", str(data.get("title", ""))[:60])
        return True

    # ── risks table ───────────────────────────────────────────────────

    async def save_risks(self, data: dict) -> bool:
        """
        Insert into risks table.
        Returns True if inserted, False if duplicate skipped.
        """
        url = data.get("url") or ""
        if url and await self._exists_risks(url):
            log.debug("[db] risks duplicate skipped: %s", url[:80])
            return False

        level_scores = {"critical": 90, "high": 70, "medium": 50, "low": 30}
        level = str(data.get("level", "medium"))
        score = int(data.get("score") or level_scores.get(level, 50))

        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO risks (
                    id, title, description, category, level, score,
                    supplier, status, last_update, scraped_at, url,
                    resource_id, impact, probability, source_reliability,
                    recommended_action, procurement_impact, market_factor
                ) VALUES (
                    $1, $2, $3, $4, $5, $6,
                    $7, $8, $9, $10, $11,
                    $12, $13, $14, $15,
                    $16, $17, $18
                )
                ON CONFLICT (id) DO NOTHING
                """,
                str(data.get("id", uuid.uuid4())),
                str(data.get("title", ""))[:500],
                str(data.get("description", "")),
                str(data.get("category", ""))[:100],
                level[:20],
                score,
                str(data.get("supplier"))[:255] if data.get("supplier") else None,
                str(data.get("status", "open"))[:20],
                data.get("last_update") or datetime.now(timezone.utc).date(),
                data.get("scraped_at") or datetime.now(timezone.utc),
                str(url) if url else None,
                str(data.get("resource_id"))[:36] if data.get("resource_id") else None,
                max(0, int(data["impact"])) if data.get("impact") is not None else None,
                max(0, int(data["probability"])) if data.get("probability") is not None else None,
                max(0, int(data["source_reliability"])) if data.get("source_reliability") is not None else None,
                str(data.get("recommended_action", "monitor"))[:20] if data.get("recommended_action") else None,
                str(data.get("procurement_impact", "medium"))[:20] if data.get("procurement_impact") else None,
                max(0, int(data["market_factor"])) if data.get("market_factor") is not None else None,
            )

        log.info("[db] risks saved: %s", str(data.get("title", ""))[:60])
        return True

    # ── alert table ───────────────────────────────────────────────────

    async def save_alert(self, data: dict) -> bool:
        """
        Insert into alert table.
        Returns True if inserted, False if duplicate skipped.

        The alert.id is GENERATED BY DEFAULT AS IDENTITY — do NOT pass it.
        """
        source_id = str(data.get("source_id", ""))
        if source_id and await self._exists_alert(source_id):
            log.debug("[db] alert duplicate skipped: %s", source_id[:32])
            return False

        prob_score = data.get("probability_score")
        if prob_score is None:
            prob_score = 50

        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO alert (
                    title, description, alert_type, category,
                    is_read, created_at, source_module, source_id, source_url,
                    supplier, regulation, regulator, event_date,
                    impact_level, recommended_action, workflow_status,
                    incident_type, cve_id, cve_url, cve_score, cyber_source,
                    assigned_buyer, priority_level, continuity_impact,
                    status_page_url, operational_source, estimated_duration,
                    backup_supplier, delivery_impact,
                    probability_score, signal_sources
                ) VALUES (
                    $1, $2, $3, $4,
                    $5, $6, $7, $8, $9,
                    $10, $11, $12, $13,
                    $14, $15, $16,
                    $17, $18, $19, $20, $21,
                    $22, $23, $24,
                    $25, $26, $27,
                    $28, $29,
                    $30, $31::jsonb
                )
                """,
                str(data.get("title", ""))[:255],
                str(data.get("description", "")),
                str(data.get("alert_type", "compliance"))[:20],
                str(data.get("category", ""))[:80],
                bool(data.get("is_read", False)),
                data.get("created_at") or datetime.now(timezone.utc),
                str(data.get("source_module", "scraping"))[:20],
                str(source_id)[:64],
                str(data.get("source_url", ""))[:500],
                str(data.get("supplier", ""))[:255],
                str(data.get("regulation", ""))[:120],
                str(data.get("regulator", ""))[:80],
                data.get("event_date"),
                str(data.get("impact_level", "medium"))[:20],
                str(data.get("recommended_action", "")),
                str(data.get("workflow_status", "open"))[:20],
                str(data.get("incident_type", ""))[:80],
                str(data.get("cve_id", ""))[:32],
                str(data.get("cve_url", ""))[:500],
                float(data["cve_score"]) if data.get("cve_score") is not None else None,
                str(data.get("cyber_source", ""))[:80],
                str(data.get("assigned_buyer", ""))[:255],
                str(data.get("priority_level", "medium"))[:10],
                str(data.get("continuity_impact", "unknown"))[:20],
                str(data.get("status_page_url", ""))[:500],
                str(data.get("operational_source", ""))[:80],
                str(data.get("estimated_duration", ""))[:120],
                str(data.get("backup_supplier", ""))[:255],
                str(data.get("delivery_impact", ""))[:120],
                int(prob_score),
                json.dumps(data.get("signal_sources") or []),
            )

        log.info("[db] alert saved: [%s] %s",
                 data.get("alert_type", "?"),
                 str(data.get("title", ""))[:60])
        return True

    # ── public_tender table ───────────────────────────────────────────

    async def save_tender(self, data: dict) -> bool:
        """
        Insert into public_tender table.
        Returns True if inserted, False if duplicate skipped.
        """
        source_url = str(data.get("source_url", ""))
        if source_url and await self._exists_tender(source_url):
            log.debug("[db] tender duplicate skipped: %s", source_url[:80])
            return False

        now = datetime.now(timezone.utc)

        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO public_tender (
                    id, title, description, buyer, country, scope, region,
                    category, cpv_codes, estimated_amount, currency,
                    procedure_type, status, published_at, deadline_at,
                    source, source_url, resource_id, it_relevance_score,
                    created_at, updated_at
                ) VALUES (
                    $1, $2, $3, $4, $5, $6, $7,
                    $8, $9::jsonb, $10, $11,
                    $12, $13, $14, $15,
                    $16, $17, $18, $19,
                    $20, $21
                )
                ON CONFLICT (id) DO NOTHING
                """,
                str(data.get("id", uuid.uuid4())),
                str(data.get("title", ""))[:500],
                str(data.get("description", "")),
                str(data.get("buyer", ""))[:255],
                str(data.get("country", "Morocco"))[:80],
                str(data.get("scope", "national"))[:20],
                str(data.get("region", ""))[:120],
                str(data.get("category", ""))[:100],
                json.dumps(data.get("cpv_codes") or []),
                float(data["estimated_amount"]) if data.get("estimated_amount") is not None else None,
                str(data.get("currency", "MAD"))[:10],
                str(data.get("procedure_type", ""))[:80],
                str(data.get("status", "open"))[:40],
                data.get("published_at"),
                data.get("deadline_at"),
                str(data.get("source", ""))[:120],
                str(source_url),
                str(data.get("resource_id"))[:36] if data.get("resource_id") else None,
                int(data.get("it_relevance_score") or 0),
                data.get("created_at") or now,
                data.get("updated_at") or now,
            )

        log.info("[db] tender saved: %s", str(data.get("title", ""))[:60])
        return True

    # ── scraping_resource table ───────────────────────────────────────

    async def upsert_scraping_resource(self, resource: dict) -> str:
        """
        Upsert a scraping resource record.
        Called at pipeline startup for each active source.
        Uses stable UUID (SHA-256 of slug) so re-runs update the same row.
        Returns the resource UUID.
        """
        slug = resource.get("slug") or resource.get("name", "")
        stable_uuid = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"scraping-resource-{slug}"))
        now = datetime.now(timezone.utc)

        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO scraping_resource (
                    id, name, base_url, description, category,
                    is_active, created_at, updated_at, resource_type,
                    consecutive_failures, last_health_message, last_health_status
                ) VALUES (
                    $1::uuid, $2, $3, $4, $5,
                    $6, $7, $8, $9,
                    0, '', 'unknown'
                )
                ON CONFLICT (id) DO UPDATE SET
                    updated_at = EXCLUDED.updated_at,
                    is_active = EXCLUDED.is_active,
                    last_health_status = scraping_resource.last_health_status
                """,
                stable_uuid,
                str(resource.get("name", ""))[:255],
                str(resource.get("base_url", ""))[:500],
                str(resource.get("description", "")),
                str(resource.get("category", "html"))[:32],
                bool(resource.get("is_active", True)),
                now,
                now,
                str(resource.get("resource_type", "html"))[:32],
            )

        return stable_uuid

    async def update_health_status(
        self,
        resource_id: str,
        status: str,
        message: str = "",
        consecutive_failures: int = 0,
    ) -> None:
        """
        Update health status of a scraping resource after each run.
        Call with status='healthy' on success, 'down' after 3+ failures.
        """
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE scraping_resource SET
                    last_health_status = $1,
                    last_health_message = $2,
                    last_health_check_at = $3,
                    consecutive_failures = $4,
                    updated_at = $3
                WHERE id = $5::uuid
                """,
                status[:32],
                message[:500],
                datetime.now(timezone.utc),
                consecutive_failures,
                resource_id,
            )

    # ── Severity update (post-classification) ─────────────────────────

    async def update_severity(self, table: str, url: str, severity: str, reason: str) -> bool:
        """
        Atomically UPDATE the severity on an existing record.

        Called after Groq classification to overwrite the placeholder 'medium'
        that was set during the initial INSERT.

        Each table stores severity in a different column:
          market → impact, impact_score, impact_detail
          risks  → level, score
          alert  → impact_level, priority_level
        """
        score = {"critical": 90, "high": 70, "medium": 50, "low": 30}.get(severity, 30)
        priority = "high" if severity in ("critical", "high") else "medium"

        if not self._pool:
            return False

        async with self._pool.acquire() as conn:
            try:
                if table == "market":
                    await conn.execute(
                        "UPDATE market SET impact = $1, impact_score = $2, impact_detail = $3 WHERE url = $4",
                        severity, score, reason, url,
                    )
                elif table == "risks":
                    await conn.execute(
                        "UPDATE risks SET level = $1, score = $2 WHERE url = $3",
                        severity, score, url,
                    )
                elif table == "alert":
                    await conn.execute(
                        "UPDATE alert SET impact_level = $1, priority_level = $2 WHERE source_url = $3",
                        severity, priority, url,
                    )
                else:
                    log.warning("[update_severity] Unknown table: %s", table)
                    return False
                log.debug("[update_severity] %s ← %s for url=%s", table, severity, url[:80])
                return True
            except Exception as exc:
                log.error("[update_severity] %s error: %s", table, exc)
                return False

    # ── Main router ───────────────────────────────────────────────────

    async def save(self, target_table: str, data: dict) -> bool:
        """
        Route to the correct save method based on target_table.
        This is the single entry point called by the pipeline runner.

        target_table: 'market' | 'risks' | 'alert' | 'public_tender'

        Returns True if saved, False if duplicate.
        Raises ValueError if target_table is unknown.
        """
        handlers = {
            "market": self.save_market,
            "risks": self.save_risks,
            "alert": self.save_alert,
            "public_tender": self.save_tender,
        }
        handler = handlers.get(target_table)
        if not handler:
            raise ValueError(
                f"Unknown target_table: '{target_table}'. "
                f"Known: {list(handlers.keys())}"
            )
        return await handler(data)
