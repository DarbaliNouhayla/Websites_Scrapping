import uuid
from datetime import datetime, timezone
from hashlib import sha256

from financial_scraper.scrapers.rss.parser import RawFeedItem
from financial_scraper.utils.text_utils import (
    CRITICAL_KEYWORDS, HIGH_KEYWORDS, MEDIUM_KEYWORDS,
    POSITIVE_KEYWORDS, NEGATIVE_KEYWORDS,
    extract_cve_id,
    extract_cve_score,
    extract_percentage,
    infer_risk_level,
    infer_trend,
    score_from_level,
    strip_html,
    truncate,
)
from financial_scraper.utils.severity import severity_from_cvss


def _matched_keywords(text: str, keywords: list[str]) -> list[str]:
    lower = text.lower()
    return [kw for kw in keywords if kw in lower]


def normalize(item: RawFeedItem) -> list[dict]:
    """Returns a list of 0-3 normalized dicts (market, risk, alert)."""
    title = truncate(strip_html(item.title), 500)
    description = truncate(strip_html(item.description), 2000)
    now = datetime.now(timezone.utc)
    published = item.published or now
    trend = infer_trend(title, description)

    target = item.extra.get("target_table", "market")
    alert_type = item.extra.get("alert_type", "")
    cve_id_from_extra = item.extra.get("cve_id", "")
    also_risks = item.extra.get("also_risks", False)

    # CISA/ENISA cyber alerts: severity from CVSS (sync, no LLM needed)
    if target == "alert" and alert_type == "cyber":
        cvss = item.extra.get("cvss_score")
        sev_result = severity_from_cvss(cvss)
        severity = sev_result["severity"]
        impact_detail = sev_result["reason"]
        results = [_to_cyber_alert(
            item, title, description, now, published, severity, cve_id_from_extra, impact_detail
        )]
        for r in results:
            r["_slug"] = item.source_slug
        return results

    risk_level = infer_risk_level(title, description)

    # Placeholder — runner will reclassify via Groq
    severity = "medium"
    impact_detail = "Pending severity classification"

    results = [_to_market(
        item, title, description, now, published, trend, severity, impact_detail
    )]

    # Media/international feeds with also_risks=True: produce risk item, NOT alert
    # Compliance feeds (target_table="alert", alert_type="compliance"): produce alert item
    if also_risks:
        results.append(_to_risks(
            item, title, description, now, published, risk_level, impact_detail
        ))

    if target == "alert" and alert_type == "compliance":
        alert = _to_regulatory_alert(
            item, title, description, now, published, severity, impact_detail
        )
        if alert:
            results.append(alert)

    for r in results:
        r["_slug"] = item.source_slug

    return results


def _to_market(
    item, title, description, now, published, trend, severity, impact_detail,
) -> dict:
    return {
        "_target_table": "market",
        "_url": item.url,
        "id": str(uuid.uuid4()),
        "title": title,
        "description": description,
        "category": item.category[:50],
        "trend": trend,
        "change": extract_percentage(title, description),
        "impact": severity,
        "impact_score": score_from_level(severity),
        "impact_detail": impact_detail,
        "url": item.url,
        "source": item.source_name[:100],
        "scraped_at": published,
        "created_at": now,
        "procurement_action": "monitor",
        "affected_suppliers": [],
    }


def _to_risks(
    item, title, description, now, published, level, impact_detail,
) -> dict:
    return {
        "_target_table": "risks",
        "_url": item.url,
        "id": str(uuid.uuid4()),
        "title": title,
        "description": description,
        "category": item.category[:100],
        "level": level,
        "score": score_from_level(level),
        "supplier": None,
        "status": "open",
        "last_update": published.date(),
        "scraped_at": now,
        "url": item.url,
        "resource_id": None,
        "impact": None,
        "probability": None,
        "source_reliability": None,
        "recommended_action": "monitor",
        "procurement_impact": None,
        "market_factor": None,
    }


def _to_cyber_alert(
    item, title, description, now, published, severity, cve_id_from_extra, impact_detail,
) -> dict:
    cve_id = cve_id_from_extra or extract_cve_id(f"{title} {description}")
    cve_score = extract_cve_score(description)
    cyber_source = item.extra.get("cyber_source", "")
    source_id = sha256(item.url.encode("utf-8")).hexdigest()[:64]

    return {
        "_target_table": "alert",
        "_url": item.url,
        "title": title[:255],
        "description": description[:2000],
        "alert_type": "cyber",
        "category": "Cybersécurité",
        "is_read": False,
        "created_at": now,
        "source_module": "scraping",
        "source_id": source_id,
        "source_url": item.url[:500],
        "supplier": "",
        "regulation": "",
        "regulator": cyber_source,
        "event_date": published.date(),
        "impact_level": severity,
        "recommended_action": "Évaluer l'impact sur les systèmes. Appliquer correctifs.",
        "workflow_status": "open",
        "incident_type": "cyber_vulnerability",
        "cve_id": cve_id,
        "cve_url": f"https://nvd.nist.gov/vuln/detail/{cve_id}" if cve_id else "",
        "cve_score": cve_score,
        "cyber_source": cyber_source,
        "assigned_buyer": "",
        "priority_level": "high" if severity in ("critical", "high") else "medium",
        "continuity_impact": "unknown",
        "status_page_url": "",
        "operational_source": "",
        "estimated_duration": "",
        "backup_supplier": "",
        "delivery_impact": "",
        "probability_score": None,
        "signal_sources": [{"source": item.source_name, "url": item.url, "scraped_at": now.isoformat()}],
    }


def _to_regulatory_alert(
    item, title, description, now, published, severity, impact_detail,
) -> dict | None:
    source_id = sha256(item.url.encode("utf-8")).hexdigest()[:64]
    return {
        "_target_table": "alert",
        "_url": item.url,
        "title": title[:255],
        "description": description[:2000],
        "alert_type": "compliance",
        "category": "Conformité réglementaire",
        "is_read": False,
        "created_at": now,
        "source_module": "scraping",
        "source_id": source_id,
        "source_url": item.url[:500],
        "supplier": "",
        "regulation": title[:120],
        "regulator": item.source_name[:80],
        "event_date": published.date(),
        "impact_level": severity,
        "recommended_action": "Évaluer l'impact réglementaire sur les opérations.",
        "workflow_status": "open",
        "incident_type": "regulatory_change",
        "cve_id": "",
        "cve_url": "",
        "cve_score": None,
        "cyber_source": "",
        "assigned_buyer": "",
        "priority_level": "high" if severity == "critical" else "medium",
        "continuity_impact": "unknown",
        "status_page_url": "",
        "operational_source": "",
        "estimated_duration": "",
        "backup_supplier": "",
        "delivery_impact": "",
        "probability_score": None,
        "signal_sources": [{"source": item.source_name, "url": item.url, "scraped_at": now.isoformat()}],
    }
