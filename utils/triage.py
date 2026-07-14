import warnings

from financial_scraper.utils.text_utils import score_from_level


def infer_severity(title: str, description: str) -> str:
    warnings.warn(
        "infer_severity() is deprecated. Use severity.get_severity() instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    return "medium"


def supplier_severity(title: str, description: str, tracked: list[str] | None = None) -> str:
    warnings.warn(
        "supplier_severity() is deprecated. Use severity.get_severity() instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    return "medium"


def regulatory_severity(title: str, description: str) -> str:
    warnings.warn(
        "regulatory_severity() is deprecated. Use severity.get_severity() instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    return "medium"


def macro_severity(title: str, description: str) -> str:
    warnings.warn(
        "macro_severity() is deprecated. Use severity.get_severity() instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    return "medium"


def cyber_severity(title: str, description: str, cvss_score: float | None = None, cyber_source: str = "") -> str:
    warnings.warn(
        "cyber_severity() is deprecated. Use severity.get_severity() or severity_from_cvss() instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    from financial_scraper.utils.severity import severity_from_cvss
    return severity_from_cvss(cvss_score)["severity"]


def triage_item(data: dict, stream: str, cvss_score: float | None = None) -> dict:
    data["impact"] = "medium"
    data["impact_score"] = score_from_level("medium")
    return data
