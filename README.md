# Financial Intelligence Platform

A platform built for a bank's third-party risk management (TPRM) team. It collects Moroccan economic, regulatory, market and cyber-threat intelligence from the web, then uses LLM-powered agents to analyze it and raise alerts. Results are served through a REST API to a Next.js frontend.

## What it does

The project is split into two independent parts that share one PostgreSQL database:

1. **Web scraping**: collects news, regulatory publications, macro indicators and cyber advisories from RSS feeds, static pages, JavaScript-heavy sites and JSON/XML sources. Each item is classified by severity (low / medium / high / critical) and saved to the database.
2. **Multi-Agent System (MAS)**: four agents read the stored data and enrich it: market trends, risk criticality and mitigation, supplier risk flags, and alert generation. A FastAPI service exposes the results.

```
Web sources ──► Scraping ──► PostgreSQL ──► MAS agents ──► FastAPI ──► Next.js
```

## Technologies by component

### 1. Web scraping

| Component | What it does | Technologies |
|---|---|---|
| RSS / JSON / sitemap feeds | Collects from 18 feeds (Moroccan media, international macro, CISA, ENISA) | `feedparser`, `httpx`, `curl_cffi` |
| Static HTML scraper | Scrapes simple server-rendered pages | `httpx`, `beautifulsoup4`, `lxml` |
| Dynamic HTML scrapers | Scrapes JavaScript-heavy and bot-protected sites (central bank, regulators, ministries, development banks, tenders) | `playwright` (Chromium, stealth mode) |
| Severity classification | Classifies items with an LLM, in batches, with caching | Groq API (`llama-3.1-8b-instant`), SQLite cache, JSON checkpoints |
| CVE enrichment | Adds CVSS scores to CISA alerts | NVD API |
| Scheduling | Runs each source on its own interval | `APScheduler` |
| Retries and resilience | Exponential backoff on failures | `tenacity` |
| Storage | Persists to PostgreSQL | `asyncpg` |
| Configuration | Loads settings from `.env` | `pydantic-settings` |

### 2. MAS agents

| Component | What it does | Technologies |
|---|---|---|
| `MarketAgent` | Trend direction, relevance to the bank, key insight | Groq API (batches of 10) |
| `RiskAgent` | Criticality score, mitigation action, human-review flag | Groq API (batches of 10) |
| `SupplierAgent` | Supplier risk flag and monitoring recommendation | Groq API (batches of 5) |
| `AlertAgent` | Builds alert rows from agent results (no LLM) | Python, SHA-256 deduplication |
| Orchestrator | Runs a full cycle and tracks status per agent | `asyncpg`, `pipeline_run` / `agent_run` tables |
| Rate-limit handling | 429 retry, daily token budget, input truncation on overflow | Custom `TokenBudget`, `tenacity` |
| REST API | Starts cycles and reports their status | `fastapi`, `uvicorn` |

## Quick start

```bash
pip install -r financial_scraper/requirements.txt
playwright install chromium
```

Create a `.env` file:

```ini
DATABASE_URL=postgresql://postgres@localhost:5433/tprm_db
GROQ_API_KEY=gsk_...
USE_DB=true
```

Run each part:

```bash
# Web scraping
python -m financial_scraper.main

# MAS agents + API (docs at http://localhost:8000/docs)
uvicorn financial_scraper.main_api:app --port 8000
```

## API

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/mas/run` | Start an analysis cycle |
| `GET` | `/api/mas/runs` | List recent cycles |
| `GET` | `/api/mas/runs/{run_id}` | Cycle details with per-agent status |
| `GET` | `/health` | Liveness probe |
