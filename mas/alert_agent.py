"""
alert_agent.py — Creates priority alerts from agent results flagged should_alert=True.

Reads results from MarketAgent, RiskAgent, SupplierAgent.
Creates rows in the alert table for items requiring immediate attention.
Does NOT call the LLM — it transforms structured agent results into alert rows.
"""

import hashlib
import logging
import uuid
from datetime import datetime, timezone

log = logging.getLogger(__name__)


class AlertAgent:
    """
    AlertAgent does not inherit BaseAgent — it does not call the LLM.
    It transforms already-analyzed results into alert table rows.
    """

    name = "AlertAgent"

    def process(self, agent_results: list[dict]) -> list[dict]:
        """
        Transform agent results that have should_alert=True into alert rows.

        Input:  list of result dicts from MarketAgent, RiskAgent, SupplierAgent
        Output: list of alert table-ready dicts
        """
        alerts = []
        now = datetime.now(timezone.utc)

        for result in agent_results:
            if not result.get("should_alert"):
                continue

            if result.get("error"):
                continue

            source_id = result.get("source_id", "")
            source_table = result.get("source_table", "")
            agent_name = result.get("agent", "")
            priority = result.get("alert_priority", "medium")
            agent_result = result.get("agent_result", {})

            # Build alert title from agent result
            key_insight = (
                agent_result.get("key_insight")
                or agent_result.get("risk_flag_reason")
                or f"Alert from {agent_name}: {source_table} item {source_id[:8]}"
            )

            # Determine alert_type from agent
            alert_type_map = {
                "MarketAgent":   "financial",
                "RiskAgent":     "operational",
                "SupplierAgent": "operational",
            }
            alert_type = alert_type_map.get(agent_name, "operational")

            # Determine incident_type
            incident_map = {
                "MarketAgent":   "market_signal",
                "RiskAgent":     "risk_escalation",
                "SupplierAgent": "supplier_risk",
            }
            incident_type = incident_map.get(agent_name, "mas_alert")

            # Build recommended_action
            action = (
                agent_result.get("recommended_action")
                or agent_result.get("mitigation_action")
                or agent_result.get("action_required")
                or "review"
            )

            # Source ID for dedup (hash of source_table + source_id + agent)
            dedup_key = f"mas_{agent_name}_{source_table}_{source_id}"
            source_id_hash = hashlib.sha256(dedup_key.encode()).hexdigest()[:64]

            # Description from agent result
            description_parts = []
            if agent_result.get("key_insight"):
                description_parts.append(agent_result["key_insight"])
            if agent_result.get("risk_flag_reason"):
                description_parts.append(agent_result["risk_flag_reason"])
            if agent_result.get("key_concerns"):
                concerns = agent_result["key_concerns"]
                if isinstance(concerns, list):
                    description_parts.append(
                        "Concerns: " + "; ".join(str(c) for c in concerns[:3])
                    )
            description = " | ".join(description_parts) or f"MAS alert from {agent_name}"

            alert_row = {
                "title": key_insight[:255],
                "description": description[:2000],
                "alert_type": alert_type,
                "category": f"MAS \u2014 {agent_name}",
                "is_read": False,
                "created_at": now,
                "source_module": "mas",
                "source_id": source_id_hash,
                "source_url": f"internal://{source_table}/{source_id}",
                "supplier": "",
                "regulation": "",
                "regulator": agent_name,
                "event_date": now.date(),
                "impact_level": priority,
                "recommended_action": str(action)[:500],
                "workflow_status": "open",
                "incident_type": incident_type,
                "cve_id": "",
                "cve_url": "",
                "cve_score": None,
                "cyber_source": "",
                "assigned_buyer": "",
                "priority_level": "high" if priority in ("critical", "high") else "medium",
                "continuity_impact": "unknown",
                "status_page_url": "",
                "operational_source": agent_name,
                "estimated_duration": "",
                "backup_supplier": "",
                "delivery_impact": "",
                "probability_score": int(
                    agent_result.get("criticality_score")
                    or agent_result.get("trend_confidence")
                    or 50
                ),
                "signal_sources": [{
                    "agent": agent_name,
                    "source_table": source_table,
                    "source_id": source_id,
                    "processed_at": result.get("processed_at", now.isoformat()),
                }],
            }

            alerts.append(alert_row)
            log.debug(
                "[AlertAgent] Created alert [%s]: %s",
                priority, key_insight[:60]
            )

        log.info("[AlertAgent] Created %d alerts from %d agent results",
                 len(alerts), len(agent_results))
        return alerts
