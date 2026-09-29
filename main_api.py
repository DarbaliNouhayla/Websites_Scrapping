from __future__ import annotations

# ── Load .env FIRST before anything else ──────────────────────────────
import os
from pathlib import Path
from dotenv import load_dotenv

# Walk up from this file to find .env — works regardless of working directory
_env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=_env_path, override=True)

# Verify immediately — if this prints NOT FOUND, .env path is wrong
_db_url = os.getenv("DATABASE_URL", "NOT FOUND")
print(f"[startup] DATABASE_URL = {_db_url}")

# ── Now import everything else ─────────────────────────────────────────
import logging
import asyncpg
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from financial_scraper.mas.api import router as mas_router
from financial_scraper.mas.api import set_pool

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
log = logging.getLogger(__name__)

app = FastAPI(
    title="AWB Intelligence Platform — MAS API",
    description="Multi-Agent System for financial intelligence analysis",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "https://your-frontend-domain.com"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(mas_router)


@app.on_event("startup")
async def startup():
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise RuntimeError("DATABASE_URL not set — check .env file")

    pool = await asyncpg.create_pool(
        db_url,
        min_size=2,
        max_size=10,
        command_timeout=60,
    )
    set_pool(pool)
    log.info("[API] Database pool connected → %s", db_url.split("@")[-1])

    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS pipeline_run (
                id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                started_at timestamptz NOT NULL DEFAULT now(),
                finished_at timestamptz,
                status varchar(30) NOT NULL DEFAULT 'RUNNING',
                articles_received integer NOT NULL DEFAULT 0,
                articles_analyzed integer NOT NULL DEFAULT 0,
                alerts_created integer NOT NULL DEFAULT 0,
                errors integer NOT NULL DEFAULT 0,
                error_details jsonb NOT NULL DEFAULT '[]',
                triggered_by varchar(50) NOT NULL DEFAULT 'api'
            );

            CREATE TABLE IF NOT EXISTS agent_run (
                id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                pipeline_run_id uuid NOT NULL REFERENCES pipeline_run(id) ON DELETE CASCADE,
                agent_name varchar(50) NOT NULL,
                status varchar(20) NOT NULL DEFAULT 'PENDING',
                started_at timestamptz,
                finished_at timestamptz,
                items_processed integer NOT NULL DEFAULT 0,
                items_skipped integer NOT NULL DEFAULT 0,
                error_message text,
                result_summary jsonb NOT NULL DEFAULT '{}'
            );

            ALTER TABLE market
                ADD COLUMN IF NOT EXISTS mas_processed boolean NOT NULL DEFAULT false,
                ADD COLUMN IF NOT EXISTS mas_processed_at timestamptz,
                ADD COLUMN IF NOT EXISTS mas_agent_result jsonb;

            ALTER TABLE risks
                ADD COLUMN IF NOT EXISTS mas_processed boolean NOT NULL DEFAULT false,
                ADD COLUMN IF NOT EXISTS mas_processed_at timestamptz,
                ADD COLUMN IF NOT EXISTS mas_agent_result jsonb;

            CREATE INDEX IF NOT EXISTS idx_market_mas
                ON market(mas_processed) WHERE mas_processed = false;

            CREATE INDEX IF NOT EXISTS idx_risks_mas
                ON risks(mas_processed) WHERE mas_processed = false;
        """)
    log.info("[API] DB migrations applied")


@app.on_event("shutdown")
async def shutdown():
    log.info("[API] Shutting down")


@app.get("/health")
async def health():
    return {"status": "ok", "service": "MAS API"}