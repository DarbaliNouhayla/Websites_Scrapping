"""
schema.py — NormalizedRecord: the standard output schema for all sources.

Every source must return NormalizedRecord before any table mapping.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class NormalizedRecord:
    # Target table hint (market_data, alert, public_tender, risk_data)
    _table: str

    # Source identity
    source_name: str          # e.g. "World Bank", "IMF"
    raw_id: str               # Original URL or GUID — used for dedup
    title: str
    content_summary: str | None = None

    # Temporal
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    # Status
    retrieval_status: str = "success"  # success | failed | retry

    # Metadata
    metadata: dict = field(default_factory=lambda: {
        "api_source": False,
        "scraper_latency_ms": 0,
    })

    # Additional fields (table-specific mappings filled downstream)
    extra: dict = field(default_factory=dict)
