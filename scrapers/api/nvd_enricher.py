
import asyncio
import logging
import os

import httpx

log = logging.getLogger(__name__)

NVD_BASE = "https://services.nvd.nist.gov/rest/json/cves/2.0"
NVD_API_KEY = os.getenv("NVD_API_KEY", "")
SLEEP_WITH_KEY = 0.7
SLEEP_WITHOUT_KEY = 6.5


def _extract_score(data: dict) -> float | None:
    try:
        vuln = data["vulnerabilities"][0]["cve"]
        metrics = vuln.get("metrics", {})
        for key in ("cvssMetricV31", "cvssMetricV30", "cvssMetricV2"):
            if key in metrics and metrics[key]:
                return float(metrics[key][0]["cvssData"]["baseScore"])
    except (KeyError, IndexError, TypeError, ValueError):
        pass
    return None


def _severity_from_score(score: float | None) -> str:
    if score is None:
        return "medium"
    if score >= 9.0:
        return "critical"
    if score >= 7.0:
        return "high"
    if score >= 4.0:
        return "medium"
    return "low"


async def enrich_cve_score(cve_id: str) -> float | None:
    if not cve_id or not cve_id.upper().startswith("CVE-"):
        return None

    headers = {"apiKey": NVD_API_KEY} if NVD_API_KEY else {}

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                NVD_BASE,
                params={"cveId": cve_id.upper()},
                headers=headers,
            )
            if resp.status_code != 200:
                log.debug("[nvd] %s \u2192 HTTP %d", cve_id, resp.status_code)
                return None
            return _extract_score(resp.json())
    except Exception as exc:
        log.debug("[nvd] Failed for %s: %s", cve_id, exc)
        return None
    finally:
        sleep = SLEEP_WITH_KEY if NVD_API_KEY else SLEEP_WITHOUT_KEY
        await asyncio.sleep(sleep)


async def enrich_cisa_items(items: list) -> list:
    to_enrich = [
        i for i in items
        if i.extra.get("cve_id") and i.extra.get("cvss_score") is None
    ]

    for item in to_enrich:
        cve_id = item.extra.get("cve_id", "")
        score = await enrich_cve_score(cve_id)
        if score is not None:
            item.extra["cvss_score"] = score
            log.debug("[nvd] %s \u2192 CVSS %.1f", cve_id, score)

    return items
