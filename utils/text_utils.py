import re


BOILERPLATE_TITLES = {
    "lire la suite", "read more", "en savoir plus", "voir plus",
    "plus", "détails", "details", "actualités", "communiqués",
}


def strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def extract_title_from_url(url_path: str) -> str:
    """Convert a URL path like /actualites/mon-article-titre to 'Mon article titre'."""
    clean = url_path.split("?")[0].split("#")[0]
    last_part = clean.rstrip("/").rsplit("/", 1)[-1] if "/" in clean else clean
    title = last_part.replace("-", " ").replace("_", " ")
    title = re.sub(r"\s+", " ", title).strip()
    if title:
        title = title[0].upper() + title[1:]
    return title


def is_boilerplate_title(text: str) -> bool:
    return text.strip().lower() in BOILERPLATE_TITLES or len(text.strip()) < 10


def truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len].rsplit(" ", 1)[0] + "..."


def extract_cve_id(text: str) -> str:
    m = re.search(r"CVE-\d{4}-\d{4,7}", text, re.IGNORECASE)
    return m.group(0).upper() if m else ""


def extract_percentage(title: str, description: str = "") -> float:
    text = f"{title} {description}"
    negative_words = (
        "baisse", "recule", "chute", "diminue", "d\u00e9cline", "r\u00e9gresse",
        "fall", "drop", "decline", "decrease", "contraction"
    )
    pattern = re.compile(r"(\d+[,\.]\d+|\d+)\s*%")
    matches = list(pattern.finditer(text))
    if not matches:
        return 0.0
    match = matches[0]
    value_str = match.group(1).replace(",", ".")
    try:
        value = float(value_str)
    except ValueError:
        return 0.0
    start = max(0, match.start() - 30)
    context = text[start:match.start()].lower()
    if any(word in context for word in negative_words):
        value = -value
    return value


def extract_cve_score(text: str) -> float | None:
    m = re.search(r"CVSS\s*(?:v?\d[\d.]*|Score)?\s*:?\s*(\d+(?:\.\d+)?)", text, re.IGNORECASE)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


POSITIVE_KEYWORDS = [
    "hausse", "croissance", "augmentation", "rise", "growth", "increase",
    "amélioration", "rebond", "progression", "expansion", "reprise",
    "boom", "record", "excédent", "bénéfice", "profit",
    "développement", "development",
    "innovation", "renouvelable", "renewable",
    "succès", "success", "résilience", "resilience",
]

NEGATIVE_KEYWORDS = [
    "baisse", "chute", "déclin", "fall", "drop", "decline",
    "contraction", "récession", "déficit", "perte", "effondrement",
    "crise", "ralentissement", "stagnation", "dégradation",
    "licenciement", "layoff", "faillite", "bankruptcy",
    "sanction", "amende", "penalty", "fine",
    "urgence", "emergency", "catastrophe", "disaster",
    "pénurie", "shortage", "inflation",
    "menace", "threat", "risque", "risk",
    "violation", "infraction",
]

CRITICAL_KEYWORDS = ["critique", "critical", "emergency", "urgence", "catastrophe", "disaster"]
HIGH_KEYWORDS = ["élevé", "high", "severe", "sévère", "grave", "majeur", "important"]
MEDIUM_KEYWORDS = ["moyen", "medium", "moderate", "modéré"]


def _keyword_score(text: str, keywords: list[str]) -> int:
    lower = text.lower()
    return sum(1 for kw in keywords if re.search(rf"\b{re.escape(kw)}\b", lower))


def infer_trend(title: str, description: str) -> str:
    combined = f"{title} {description}"
    pos = _keyword_score(combined, POSITIVE_KEYWORDS)
    neg = _keyword_score(combined, NEGATIVE_KEYWORDS)
    if pos > neg:
        return "up"
    if neg > pos:
        return "down"
    return "stable"


def infer_severity(title: str, description: str) -> str:
    combined = f"{title} {description}"
    if _keyword_score(combined, CRITICAL_KEYWORDS) > 0:
        return "critical"
    if _keyword_score(combined, HIGH_KEYWORDS) > 0:
        return "high"
    if _keyword_score(combined, MEDIUM_KEYWORDS) > 0:
        return "medium"
    return "low"


def infer_risk_level(title: str, description: str) -> str:
    return infer_severity(title, description)


def score_from_level(level: str) -> int | None:
    return {"critical": 90, "high": 70, "medium": 50, "low": 30}.get(level, 30)
