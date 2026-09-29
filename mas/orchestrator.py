"""
orchestrator.py — MASOrchestrator: coordinates all 4 agents.

Cycle flow:
1. Read unprocessed market rows (mas_processed=false)
2. Read unprocessed risks rows (mas_processed=false)
3. Read fournisseur_equipement rows + cross-reference with recent articles
4. Run MarketAgent on market items
5. Run RiskAgent on risks items
6. Run SupplierAgent on supplier+article bundles
7. Run AlertAgent on all results flagged should_alert=True
8. Save all results to DB
9. Mark processed rows as mas_processed=true
10. Close pipeline_run with final status
"""

from __future__ import annotations
import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone

import asyncpg

from .base_agent import BaseAgent
from .market_agent import MarketAgent
from .risk_agent import RiskAgent
from .supplier_agent import SupplierAgent
from .alert_agent import AlertAgent

log = logging.getLogger(__name__)

# Max items per agent per cycle — prevents runaway cycles
MAX_MARKET_ITEMS = 10
MAX_RISK_ITEMS = 10
MAX_SUPPLIER_ITEMS = 10

# Groq rate limiting between agent calls
AGENT_SLEEP_SECONDS = 2.1


class MASOrchestrator:
    """
    Central orchestrator for the multi-agent system.
    One instance per cycle. Create a new one for each run.
    """

    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool
        self._market_agent = MarketAgent()
        self._risk_agent = RiskAgent()
        self._supplier_agent = SupplierAgent()
        self._alert_agent = AlertAgent()

    # ── Data fetching ──────────────────────────────────────────────────

    async def _fetch_unprocessed_market(self, limit: int) -> list[dict]:
        """Fetch market rows not yet analyzed by MAS."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, title, description, source, category,
                       impact, trend, url, scraped_at
                FROM market
                WHERE mas_processed = false
                  AND title IS NOT NULL
                  AND title != ''
                ORDER BY scraped_at DESC NULLS LAST
                LIMIT $1
                """,
                limit,
            )
        return [dict(r) for r in rows]

    async def _fetch_unprocessed_risks(self, limit: int) -> list[dict]:
        """Fetch risks rows not yet analyzed by MAS."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, title, description, category, level, score,
                       url, scraped_at
                FROM risks
                WHERE mas_processed = false
                  AND title IS NOT NULL
                ORDER BY scraped_at DESC NULLS LAST
                LIMIT $1
                """,
                limit,
            )
        return [dict(r) for r in rows]

    async def _fetch_suppliers_with_articles(self, limit: int) -> list[dict]:
        """
        Fetch suppliers and find recent market/risks articles mentioning them.
        Returns list of {supplier: dict, related_articles: list[dict]}
        """
        async with self._pool.acquire() as conn:
            suppliers = await conn.fetch(
                """
                SELECT id, name, sector, categories, aliases,
                       supplier_score, delivery_risk, financial_risk,
                       financial_health_score, financial_health_trend,
                       financial_default_risk, awb_dependency_level,
                       news_sentiment
                FROM fournisseur_equipement
                ORDER BY
                    CASE awb_dependency_level
                        WHEN 'critical' THEN 1
                        WHEN 'high' THEN 2
                        WHEN 'medium' THEN 3
                        ELSE 4
                    END,
                    last_analysis ASC NULLS FIRST
                LIMIT $1
                """,
                limit,
            )

        result = []
        for supplier in suppliers:
            sup_dict = dict(supplier)
            supplier_name = sup_dict.get("name", "")

            # Get aliases for broader search
            aliases = sup_dict.get("aliases") or []
            if isinstance(aliases, str):
                try:
                    aliases = json.loads(aliases)
                except Exception:
                    aliases = []

            search_terms = [supplier_name] + [str(a) for a in aliases[:3]]

            # Find related articles mentioning this supplier
            related = []
            async with self._pool.acquire() as conn:
                for term in search_terms[:2]:  # limit to 2 terms to avoid slowness
                    if len(term) < 3:
                        continue
                    rows = await conn.fetch(
                        """
                        SELECT id, title, source, impact, scraped_at
                        FROM market
                        WHERE (title ILIKE $1 OR description ILIKE $1)
                          AND scraped_at > NOW() - INTERVAL '30 days'
                        LIMIT 3
                        """,
                        f"%{term}%",
                    )
                    related.extend([dict(r) for r in rows])

            result.append({
                "supplier": sup_dict,
                "related_articles": related[:5],
            })

        return result

    # ── DB writers ─────────────────────────────────────────────────────

    async def _mark_processed(
        self,
        table: str,
        ids: list[str],
        results_by_id: dict[str, dict],
    ) -> None:
        """Mark rows as mas_processed=true and store agent result."""
        if not ids:
            return
        now = datetime.now(timezone.utc)
        async with self._pool.acquire() as conn:
            for row_id in ids:
                agent_result = results_by_id.get(str(row_id), {})
                await conn.execute(
                    f"""
                    UPDATE {table}
                    SET mas_processed = true,
                        mas_processed_at = $1,
                        mas_agent_result = $2::jsonb
                    WHERE id = $3
                    """,
                    now,
                    json.dumps(agent_result),
                    str(row_id),
                )

    async def _save_alert(self, alert_data: dict) -> bool:
        """Save one alert to the alert table. Skip if duplicate source_id."""
        async with self._pool.acquire() as conn:
            existing = await conn.fetchrow(
                "SELECT id FROM alert WHERE source_id = $1 LIMIT 1",
                alert_data["source_id"],
            )
            if existing:
                return False

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
                    $1,$2,$3,$4,$5,$6,$7,$8,$9,
                    $10,$11,$12,$13,$14,$15,$16,
                    $17,$18,$19,$20,$21,
                    $22,$23,$24,$25,$26,$27,$28,$29,
                    $30,$31::jsonb
                )
                """,
                alert_data["title"],
                alert_data["description"],
                alert_data["alert_type"],
                alert_data["category"],
                alert_data["is_read"],
                alert_data["created_at"],
                alert_data["source_module"],
                alert_data["source_id"],
                alert_data["source_url"],
                alert_data["supplier"],
                alert_data["regulation"],
                alert_data["regulator"],
                alert_data["event_date"],
                alert_data["impact_level"],
                alert_data["recommended_action"][:2000],
                alert_data["workflow_status"],
                alert_data["incident_type"],
                alert_data["cve_id"],
                alert_data["cve_url"],
                alert_data.get("cve_score"),
                alert_data["cyber_source"],
                alert_data["assigned_buyer"],
                alert_data["priority_level"],
                alert_data["continuity_impact"],
                alert_data["status_page_url"],
                alert_data["operational_source"],
                alert_data["estimated_duration"],
                alert_data["backup_supplier"],
                alert_data["delivery_impact"],
                alert_data["probability_score"],
                json.dumps(alert_data["signal_sources"]),
            )
        return True

    async def _update_pipeline_run(
        self,
        run_id: str,
        **kwargs,
    ) -> None:
        """Update pipeline_run row with new values."""
        set_clauses = ", ".join(f"{k} = ${i+2}" for i, k in enumerate(kwargs))
        values = list(kwargs.values())
        async with self._pool.acquire() as conn:
            await conn.execute(
                f"UPDATE pipeline_run SET {set_clauses} WHERE id = $1::uuid",
                run_id,
                *values,
            )

    async def _update_agent_run(
        self,
        agent_run_id: str,
        **kwargs,
    ) -> None:
        """Update agent_run row with new values."""
        set_clauses = ", ".join(f"{k} = ${i+2}" for i, k in enumerate(kwargs))
        values = list(kwargs.values())
        async with self._pool.acquire() as conn:
            await conn.execute(
                f"UPDATE agent_run SET {set_clauses} WHERE id = $1::uuid",
                agent_run_id,
                *values,
            )

    # ── Agent runner helper ────────────────────────────────────────────

    async def _run_agent(
        self,
        pipeline_run_id: str,
        agent: BaseAgent | AlertAgent,
        items: list[dict],
        is_alert_agent: bool = False,
    ) -> tuple[list[dict], str]:
        """
        Run one agent, track its agent_run record, handle errors.
        Returns (results, agent_run_id).
        """
        agent_run_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)

        # Create agent_run record
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO agent_run (
                    id, pipeline_run_id, agent_name, status, started_at
                ) VALUES ($1::uuid, $2::uuid, $3, 'RUNNING', $4)
                """,
                agent_run_id,
                pipeline_run_id,
                agent.name,
                now,
            )

        try:
            if is_alert_agent:
                results = agent.process(items)  # sync, no LLM
            else:
                results = await agent.process(items)

            success_count = sum(1 for r in results if not r.get("error"))
            error_count = sum(1 for r in results if r.get("error"))

            await self._update_agent_run(
                agent_run_id,
                status="SUCCESS" if error_count == 0 else "SUCCESS",
                finished_at=datetime.now(timezone.utc),
                items_processed=success_count,
                items_skipped=error_count,
                result_summary=json.dumps({
                    "success": success_count,
                    "errors": error_count,
                }),
            )

            log.info(
                "[Orchestrator] %s completed: %d success, %d errors",
                agent.name, success_count, error_count,
            )
            return results, agent_run_id

        except Exception as exc:
            log.error("[Orchestrator] %s FAILED: %s", agent.name, exc)
            await self._update_agent_run(
                agent_run_id,
                status="FAILED",
                finished_at=datetime.now(timezone.utc),
                error_message=str(exc)[:1000],
            )
            return [], agent_run_id

    # ── Main cycle ─────────────────────────────────────────────────────

    async def run_cycle(
        self,
        triggered_by: str = "api",
    ) -> dict:
        """
        Execute one full MAS cycle.

        Returns summary dict:
        {
            "run_id": "...",
            "status": "SUCCESS|SUCCESS_WITH_WARNINGS|FAILED",
            "articles_received": N,
            "articles_analyzed": N,
            "alerts_created": N,
            "errors": N,
            "duration_seconds": N,
            "agent_statuses": {...}
        }
        """
        run_id = str(uuid.uuid4())
        started_at = datetime.now(timezone.utc)
        errors = []
        all_agent_results = []

        log.info("[Orchestrator] Starting cycle %s", run_id)

        # Create pipeline_run record
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO pipeline_run (id, started_at, status, triggered_by)
                VALUES ($1::uuid, $2, 'RUNNING', $3)
                """,
                run_id, started_at, triggered_by,
            )

        try:
            # ── Step 1: Fetch data ──────────────────────────────────────
            log.info("[Orchestrator] Fetching unprocessed items...")
            market_items    = await self._fetch_unprocessed_market(MAX_MARKET_ITEMS)
            risk_items      = await self._fetch_unprocessed_risks(MAX_RISK_ITEMS)
            supplier_items  = await self._fetch_suppliers_with_articles(MAX_SUPPLIER_ITEMS)

            total_received = len(market_items) + len(risk_items) + len(supplier_items)
            log.info(
                "[Orchestrator] Fetched: market=%d risks=%d suppliers=%d",
                len(market_items), len(risk_items), len(supplier_items),
            )

            await self._update_pipeline_run(
                run_id, articles_received=total_received
            )

            # ── Step 2: MarketAgent ────────────────────────────────────
            if market_items:
                market_results, _ = await self._run_agent(
                    run_id, self._market_agent, market_items
                )
                all_agent_results.extend(market_results)

                # Mark market items processed
                results_by_id = {r["source_id"]: r["agent_result"]
                                 for r in market_results if not r.get("error")}
                await self._mark_processed(
                    "market",
                    [str(i["id"]) for i in market_items],
                    results_by_id,
                )
                await asyncio.sleep(AGENT_SLEEP_SECONDS)

            # ── Step 3: RiskAgent ──────────────────────────────────────
            if risk_items:
                risk_results, _ = await self._run_agent(
                    run_id, self._risk_agent, risk_items
                )
                all_agent_results.extend(risk_results)

                results_by_id = {r["source_id"]: r["agent_result"]
                                 for r in risk_results if not r.get("error")}
                await self._mark_processed(
                    "risks",
                    [str(i["id"]) for i in risk_items],
                    results_by_id,
                )
                await asyncio.sleep(AGENT_SLEEP_SECONDS)

            # ── Step 4: SupplierAgent ──────────────────────────────────
            if supplier_items:
                supplier_results, _ = await self._run_agent(
                    run_id, self._supplier_agent, supplier_items
                )
                all_agent_results.extend(supplier_results)
                await asyncio.sleep(AGENT_SLEEP_SECONDS)

            # ── Step 5: AlertAgent ─────────────────────────────────────
            alerts_to_create = [r for r in all_agent_results if r.get("should_alert")]

            if alerts_to_create:
                alert_rows, _ = await self._run_agent(
                    run_id,
                    self._alert_agent,
                    all_agent_results,
                    is_alert_agent=True,
                )

                alerts_created = 0
                for alert_row in alert_rows:
                    try:
                        saved = await self._save_alert(alert_row)
                        if saved:
                            alerts_created += 1
                    except Exception as exc:
                        log.error("[Orchestrator] Alert save failed: %s", exc)
                        errors.append(str(exc))
            else:
                alerts_created = 0
                log.info("[Orchestrator] No items flagged for alerts")

            # ── Step 6: Close cycle ────────────────────────────────────
            total_analyzed = sum(
                1 for r in all_agent_results if not r.get("error")
            )
            error_count = sum(1 for r in all_agent_results if r.get("error"))
            error_count += len(errors)

            final_status = (
                "FAILED" if total_analyzed == 0 and total_received > 0
                else "SUCCESS_WITH_WARNINGS" if error_count > 0
                else "SUCCESS"
            )

            finished_at = datetime.now(timezone.utc)
            duration = (finished_at - started_at).total_seconds()

            await self._update_pipeline_run(
                run_id,
                finished_at=finished_at,
                status=final_status,
                articles_received=total_received,
                articles_analyzed=total_analyzed,
                alerts_created=alerts_created,
                errors=error_count,
                error_details=json.dumps(errors[-10:]),
            )

            summary = {
                "run_id": run_id,
                "status": final_status,
                "articles_received": total_received,
                "articles_analyzed": total_analyzed,
                "alerts_created": alerts_created,
                "errors": error_count,
                "duration_seconds": round(duration, 1),
            }

            log.info(
                "[Orchestrator] Cycle %s completed: %s | analyzed=%d alerts=%d errors=%d duration=%.1fs",
                run_id, final_status, total_analyzed, alerts_created,
                error_count, duration,
            )
            return summary

        except Exception as exc:
            log.error("[Orchestrator] Cycle FAILED with unhandled exception: %s", exc)
            await self._update_pipeline_run(
                run_id,
                finished_at=datetime.now(timezone.utc),
                status="FAILED",
                error_details=json.dumps([str(exc)]),
            )
            raise
