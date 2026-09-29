"""
market_agent.py — Analyzes market articles using batch Groq calls.
"""

from __future__ import annotations
import logging
from .base_agent import BaseAgent

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """
You are a market intelligence analyst for Attijariwafa Bank (AWB), Morocco's largest bank.
Analyze the given market article and return a JSON object with:

{
  "trend_direction": "up|down|stable",
  "trend_confidence": 0-100,
  "awb_relevance": "direct|indirect|none",
  "awb_impact_areas": ["banking", "credit", "forex", "commodity", "regulation", "competitor"],
  "key_insight": "one sentence summary in the article's language (fr or en)",
  "recommended_action": "monitor|investigate|escalate|ignore",
  "sectors_affected": ["finance", "energy", "transport", "telecom", "agriculture", "other"],
  "geographic_scope": "morocco|africa|europe|global",
  "time_sensitivity": "immediate|short_term|long_term"
}

Rules:
- awb_relevance=direct: directly affects AWB or Moroccan banking sector
- awb_relevance=indirect: affects AWB suppliers, clients, or regional economy
- awb_relevance=none: global news with no Morocco connection
- recommended_action=escalate only for critical regulatory or financial stability news
- key_insight max 150 chars
"""


class MarketAgent(BaseAgent):
    name = "MarketAgent"
    max_tokens = 250
    DEFAULT_BATCH_SIZE = 10

    async def process(self, items: list[dict]) -> list[dict]:
        """
        Analyze market articles using batch Groq calls.
        10 items per call instead of 1 — 10x faster.
        """
        # Check and trim for token budget
        items = self._check_and_trim_budget(items)
        if not items:
            return []

        results = []

        # Build user content strings for all items
        user_contents = []
        for item in items:
            user_contents.append(
                f"Source: {item.get('source', '')}\n"
                f"Category: {item.get('category', '')}\n"
                f"Title: {item.get('title', '')[:150]}\n"
                f"Description: {item.get('description', '')[:200]}"
            )

        # Batch classify — one Groq call per 10 items
        analyses = await self._call_llm_batch(
            system_prompt=SYSTEM_PROMPT,
            user_contents=user_contents,
            batch_size=self.DEFAULT_BATCH_SIZE,
        )

        # Record token usage
        self._record_usage(len(items))

        # Map results back to items
        for item, analysis in zip(items, analyses):
            item_id = item.get("id", "")

            if not analysis:
                results.append(
                    self._error_result(item_id, "market", "LLM returned null")
                )
                continue

            # Remove batch id field if present
            analysis.pop("id", None)

            should_alert = (
                analysis.get("recommended_action") == "escalate"
                or (
                    analysis.get("awb_relevance") == "direct"
                    and analysis.get("trend_confidence", 0) > 70
                )
            )
            priority = (
                "high" if analysis.get("awb_relevance") == "direct"
                else "medium" if analysis.get("awb_relevance") == "indirect"
                else "low"
            )

            results.append(self._safe_result(
                source_id=item_id,
                source_table="market",
                agent_result=analysis,
                should_alert=should_alert,
                alert_priority=priority,
            ))

        success = sum(1 for r in results if not r.get("error"))
        log.info("[MarketAgent] Processed %d/%d items successfully",
                 success, len(items))
        return results
