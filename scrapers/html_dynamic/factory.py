from financial_scraper.scrapers.html_dynamic.bam import BAMScraper
from financial_scraper.scrapers.html_dynamic.ammc import AMMCScraper
from financial_scraper.scrapers.html_dynamic.acaps import ACAPSScraper
from financial_scraper.scrapers.html_dynamic.mef import MEFScraper
from financial_scraper.scrapers.html_dynamic.anrt import ANRTScraper
from financial_scraper.scrapers.html_dynamic.cgem import CGEMScraper
from financial_scraper.scrapers.html_dynamic.ompic import OMPICScraper
from financial_scraper.scrapers.html_dynamic.worldbank import WorldBankScraper
from financial_scraper.scrapers.html_dynamic.afdb import AfDBScraper
from financial_scraper.scrapers.html_dynamic.ebrd import EBRDScraper

_REGISTRY = {
    "bam": BAMScraper,
    "ammc": AMMCScraper,
    "acaps": ACAPSScraper,
    "mef": MEFScraper,
    "anrt": ANRTScraper,
    "cgem": CGEMScraper,
    "ompic": OMPICScraper,
    "worldbank": WorldBankScraper,
    "afdb": AfDBScraper,
    "ebrd": EBRDScraper,
}


class DynamicScraperFactory:
    @staticmethod
    def get(slug: str) -> object:
        cls = _REGISTRY.get(slug)
        if not cls:
            registered = ", ".join(_REGISTRY.keys())
            raise ValueError(f"Unknown dynamic scraper slug: '{slug}'. Registered: {registered}")
        return cls()
