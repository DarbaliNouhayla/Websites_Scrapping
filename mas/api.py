"""
api.py — FastAPI router exposing 3 MAS endpoints.

POST /api/mas/run              — start a new cycle
GET  /api/mas/runs             — list all past cycles
GET  /api/mas/runs/{run_id}    — detail of one cycle with agent statuses
"""

from __future__ import annotations
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, BackgroundTasks
from pydantic import BaseModel

from .orchestrator import MASOrchestrator

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/mas", tags=["MAS"])

# Pool injected at startup from main_api.py
_pool = None


def set_pool(pool):
    global _pool
    _pool = pool


# ── Request/Response models ───────────────────────────────────────────

class RunRequest(BaseModel):
    triggered_by: str = "api"


class RunResponse(BaseModel):
    run_id: str
    status: str
    message: str


# ── Background task ───────────────────────────────────────────────────

async def _execute_cycle(triggered_by: str):
    """Background task — runs the MAS cycle without blocking the API response."""
    try:
        orchestrator = MASOrchestrator(pool=_pool)
        await orchestrator.run_cycle(triggered_by=triggered_by)
    except Exception as exc:
        log.error("[API] Background cycle failed: %s", exc)


# ── Endpoints ─────────────────────────────────────────────────────────

@router.post("/run", response_model=RunResponse, status_code=202)
async def start_cycle(
    request: RunRequest,
    background_tasks: BackgroundTasks,
):
    """
    Start a new MAS analysis cycle.
    Returns immediately with run_id.
    Cycle runs in background — poll /runs/{run_id} for status.
    """
    if _pool is None:
        raise HTTPException(status_code=503, detail="Database not connected")

    # Check if a cycle is already running
    async with _pool.acquire() as conn:
        running = await conn.fetchrow(
            "SELECT id FROM pipeline_run WHERE status = 'RUNNING' LIMIT 1"
        )
        if running:
            raise HTTPException(
                status_code=409,
                detail=f"A cycle is already running: {running['id']}"
            )

    # Start cycle in background
    background_tasks.add_task(_execute_cycle, request.triggered_by)

    # Get the run_id that will be created (we need to pre-create it)
    # Actually just return a message — client polls /runs for latest
    return RunResponse(
        run_id="pending",
        status="ACCEPTED",
        message="MAS cycle started. Poll /api/mas/runs for status.",
    )


@router.get("/runs")
async def list_runs(limit: int = 20):
    """
    List recent pipeline runs with their summary.
    """
    if _pool is None:
        raise HTTPException(status_code=503, detail="Database not connected")

    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, started_at, finished_at, status,
                   articles_received, articles_analyzed,
                   alerts_created, errors, triggered_by
            FROM pipeline_run
            ORDER BY started_at DESC
            LIMIT $1
            """,
            limit,
        )

    return {
        "runs": [dict(r) for r in rows],
        "total": len(rows),
    }


@router.get("/runs/{run_id}")
async def get_run(run_id: str):
    """
    Get detailed status of one pipeline run including all agent statuses.
    """
    if _pool is None:
        raise HTTPException(status_code=503, detail="Database not connected")

    async with _pool.acquire() as conn:
        run = await conn.fetchrow(
            "SELECT * FROM pipeline_run WHERE id = $1::uuid", run_id
        )
        if not run:
            raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

        agents = await conn.fetch(
            """
            SELECT agent_name, status, started_at, finished_at,
                   items_processed, items_skipped, error_message,
                   result_summary
            FROM agent_run
            WHERE pipeline_run_id = $1::uuid
            ORDER BY started_at ASC NULLS LAST
            """,
            run_id,
        )

    return {
        "run": dict(run),
        "agents": [dict(a) for a in agents],
    }
