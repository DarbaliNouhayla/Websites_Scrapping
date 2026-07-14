from financial_scraper.scrapers.html_static.medias24 import Medias24Scraper

_REGISTRY = {
    "medias24": Medias24Scraper,
}


class HTMLScraperFactory:
    @staticmethod
    def get(slug: str) -> object:
        cls = _REGISTRY.get(slug)
        if not cls:
            registered = ", ".join(_REGISTRY.keys())
            raise ValueError(f"Unknown static scraper slug: '{slug}'. Registered: {registered}")
        return cls()
