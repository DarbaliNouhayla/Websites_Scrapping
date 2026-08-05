"""
supplier_agent.py — Analyzes suppliers using batch Groq calls.
"""

from __future__ import annotations
import json
import logging
from .base_agent import BaseAgent

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """
You are a supplier risk analyst for Attijariwafa Bank (AWB), Morocco.
You receive a supplier profile and recent news about them.
Return a JSON object with:

{
  "risk_flag": "critical|high|medium|low|none",
  "risk_flag_reason": "one sentence in French explaining the flag",
  "news_sentiment_update": -1.0,
  "recommended_monitoring": "daily|weekly|monthly|standard",
  "supplier_health": "deteriorating|stable|improving",
  "key_concerns": ["specific concerns from news"],
  "positive_signals": ["positive signals from news"],
  "action_required": "escalate_to_buyer|review_contract|increase_monitoring|no_action",
  "requires_human_review": true|false
}

Rules:
- risk_flag=critical: active sanctions, bankruptcy news, confirmed fraud
- risk_flag=high: payment incidents, financial distress, regulatory violations
- news_sentiment_update: -1.0=very negative, 0=neutral, 1.0=very positive
- requires_human_review=true when risk_flag is critical or high
- Write risk_flag_reason in French
"""


class SupplierAgent(BaseAgent):
    name = "SupplierAgent"
    max_tokens = 350
    DEFAULT_BATCH_SIZE = 5  # smaller batches — supplier content is longer

    async def process(self, items: list[dict]) -> list[dict]:
        """
        Analyze suppliers cross-referenced with recent news.
        Uses smaller batches (5) because supplier profiles are longer.
        """
        items = self._check_and_trim_budget(items)
        if not items:
            return []

        results = []

        user_contents = []
        for item in items:
            supplier = item.get("supplier", {})
            related_articles = item.get("related_articles", [])

            profile = (
                f"Supplier: {supplier.get('name', '')}\n"
                f"Sector: {supplier.get('sector', '')}\n"
                f"AWB dependency: {supplier.get('awb_dependency_level', 'medium')}\n"
                f"Supplier score: {supplier.get('supplier_score', 0)}/100\n"
                f"Financial health: {supplier.get('financial_health_score', 0)}/100\n"
                f"Financial trend: {supplier.get('financial_health_trend', 'stable')}\n"
            )

            if related_articles:
                news = "\nRecent news:\n" + "\n".join(
                    f"- [{a.get('source','?')}] {a.get('title','')[:80]}"
                    for a in related_articles[:3]
                )
            else:
                news = "\nNo recent news found."

            user_contents.append(profile + news)

        analyses = await self._call_llm_batch(
            system_prompt=SYSTEM_PROMPT,
            user_contents=user_contents,
            batch_size=self.DEFAULT_BATCH_SIZE,
        )

        self._record_usage(len(items))

        for item, analysis in zip(items, analyses):
            supplier = item.get("supplier", {})
            supplier_id = supplier.get("id", "")

            if not analysis:
                results.append(
                    self._error_result(
                        supplier_id,
                        "fournisseur_equipement",
                        "LLM returned null",
                    )
                )
                continue

            analysis.pop("id", None)

            risk_flag = analysis.get("risk_flag", "none")
            should_alert = risk_flag in ("critical", "high")
            priority = risk_flag if risk_flag != "none" else "low"

            results.append(self._safe_result(
                source_id=supplier_id,
                source_table="fournisseur_equipement",
                agent_result=analysis,
                should_alert=should_alert,
                alert_priority=priority,
            ))

        success = sum(1 for r in results if not r.get("error"))
        log.info("[SupplierAgent] Processed %d/%d suppliers successfully",
                 success, len(items))
        return results
