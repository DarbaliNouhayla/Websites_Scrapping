"""
risk_agent.py — Analyzes risk articles using batch Groq calls.
"""

from __future__ import annotations
import logging
from .base_agent import BaseAgent

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """
You are a Third-Party Risk Manager for Attijariwafa Bank (AWB), Morocco.
Analyze the given risk article and return a JSON object with:

{
  "criticality": "critical|high|medium|low",
  "criticality_score": 0-100,
  "risk_category": "cyber|compliance|operational|financial|geopolitical|supply_chain",
  "affected_supplier_types": ["IT", "telecom", "logistics", "energy", "financial", "other"],
  "probability": 0-100,
  "impact_on_awb": "direct|indirect|none",
  "time_to_impact": "immediate|days|weeks|months",
  "mitigation_action": "block_supplier|review_contract|monitor|alert_compliance|no_action",
  "key_insight": "one sentence in article language (fr or en)",
  "requires_human_review": true|false
}

Rules:
- criticality=critical: requires immediate human escalation
- requires_human_review=true when criticality is critical or high AND impact_on_awb is direct
- mitigation_action=block_supplier only for confirmed fraud or sanctions
"""


class RiskAgent(BaseAgent):
    name = "RiskAgent"
    max_tokens = 280
    DEFAULT_BATCH_SIZE = 10

    async def process(self, items: list[dict]) -> list[dict]:
        """
        Analyze risk items using batch Groq calls.
        """
        items = self._check_and_trim_budget(items)
        if not items:
            return []

        results = []

        user_contents = []
        for item in items:
            user_contents.append(
                f"Risk category: {item.get('category', '')}\n"
                f"Current level: {item.get('level', '')}\n"
                f"Title: {item.get('title', '')[:150]}\n"
                f"Description: {item.get('description', '')[:200]}"
            )

        analyses = await self._call_llm_batch(
            system_prompt=SYSTEM_PROMPT,
            user_contents=user_contents,
            batch_size=self.DEFAULT_BATCH_SIZE,
        )

        self._record_usage(len(items))

        for item, analysis in zip(items, analyses):
            item_id = item.get("id", "")

            if not analysis:
                results.append(
                    self._error_result(item_id, "risks", "LLM returned null")
                )
                continue

            analysis.pop("id", None)

            should_alert = (
                analysis.get("criticality") in ("critical", "high")
                and analysis.get("impact_on_awb") != "none"
            )
            priority = analysis.get("criticality", "low")

            results.append(self._safe_result(
                source_id=item_id,
                source_table="risks",
                agent_result=analysis,
                should_alert=should_alert,
                alert_priority=priority,
            ))

        success = sum(1 for r in results if not r.get("error"))
        log.info("[RiskAgent] Processed %d/%d items successfully",
                 success, len(items))
        return results
