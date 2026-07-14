import asyncio
import logging
from dotenv import load_dotenv
load_dotenv()

from financial_scraper.config.settings import Settings, settings as cfg
from financial_scraper.utils.logger import configure_logging

log = logging.getLogger("financial_scraper.main")


async def run_rss_pipeline(repo, settings: Settings, dev_mode: bool, checkpoint, resource_map):
    """Run RSS pipeline with inline Groq classification."""
    from financial_scraper.scrapers.rss.runner import RSSPipeline
    from financial_scraper.utils.dedup import UrlDeduplicator

    rss = RSSPipeline(
        repository=repo,
        dedup=UrlDeduplicator(),
        dev_mode=dev_mode,
        checkpoint=checkpoint,
        resource_map=resource_map,
    )
    try:
        await rss.run_unsafe()
    except Exception as exc:
        log.error("[PIPELINE] RSS pipeline failed: %s", exc)
    finally:
        await rss.close()


async def run_static_pipeline(repo, settings: Settings, dev_mode: bool, checkpoint, resource_map):
    from financial_scraper.fetchers.strategies import StaticFetchStrategy
    from financial_scraper.scrapers.html_static.factory import HTMLScraperFactory
    from financial_scraper.scrapers.engine import IntelligentEngine
    from financial_scraper.utils.dedup import UrlDeduplicator
    from financial_scraper.utils.severity import classify_severity
    from financial_scraper.utils.text_utils import score_from_level

    static_strategy = StaticFetchStrategy()
    static_engine = IntelligentEngine(
        strategy=static_strategy,
        repository=repo,
        dedup=UrlDeduplicator(),
        rate_limit=settings.rate_limit_static,
        dev_mode=dev_mode,
        resource_map=resource_map,
    )
    try:
        for slug in ["medias24"]:
            try:
                scraper = HTMLScraperFactory.get(slug)
                results = await scraper.scrape(static_strategy)
                for item in results:
                    target = item.get("_target_table", "market")
                    data = {k: v for k, v in item.items() if k != "_target_table"}
                    title = data.get("title", "")
                    description = data.get("description", "")
                    if title and len(title) >= 3:
                        try:
                            sev = await classify_severity(
                                title=title,
                                description=description,
                                source=slug,
                                cache=checkpoint,
                            )
                            data["impact"] = sev["severity"]
                            data["impact_score"] = score_from_level(sev["severity"])
                            data["impact_detail"] = sev.get("reason", "")
                            log.debug("[severity:%s] %s \u2192 %s", slug, title[:50], sev["severity"])
                            await asyncio.sleep(2.1)
                        except Exception as exc:
                            log.warning("[severity:%s] Groq failed: %s", slug, exc)
                            data["impact"] = "medium"
                            data["impact_detail"] = "Groq classification failed"
                    await static_engine.save_result(target, data)
                    await asyncio.sleep(settings.rate_limit_static)
                await static_engine.update_health(slug, "healthy")
            except Exception as exc:
                log.error("[%s] Static scrape error: %s", slug, exc)
                if repo and resource_map:
                    rid = resource_map.get(slug)
                    if rid:
                        await repo.update_health_status(rid, "down", str(exc)[:200], 1)
    except Exception as exc:
        log.error("[PIPELINE] Static HTML pipeline failed: %s", exc)
    finally:
        await static_engine.close()


async def run_dynamic_pipeline(repo, settings: Settings, dev_mode: bool, resource_map):
    from financial_scraper.fetchers.strategies import DynamicFetchStrategy
    from financial_scraper.scrapers.html_dynamic.factory import DynamicScraperFactory
    from financial_scraper.scrapers.engine import IntelligentEngine
    from financial_scraper.utils.dedup import UrlDeduplicator

    dynamic_strategy = DynamicFetchStrategy(headless=settings.playwright_headless)
    dynamic_engine = IntelligentEngine(
        strategy=dynamic_strategy,
        repository=repo,
        dedup=UrlDeduplicator(),
        rate_limit=settings.rate_limit_dynamic,
        dev_mode=dev_mode,
        resource_map=resource_map,
    )
    try:
        for slug in ["bam", "ammc", "acaps", "mef", "anrt", "cgem", "worldbank", "afdb", "ebrd"]:
            try:
                scraper = DynamicScraperFactory.get(slug)
                results = await scraper.scrape(dynamic_strategy)
                for item in results:
                    target = item.get("_target_table", "alert")
                    data = {k: v for k, v in item.items() if k != "_target_table"}
                    await dynamic_engine.save_result(target, data)
                    await asyncio.sleep(settings.rate_limit_dynamic)
                await dynamic_engine.update_health(slug, "healthy")
            except Exception as exc:
                log.error("[%s] Dynamic scrape error: %s", slug, exc)
                if repo and resource_map:
                    rid = resource_map.get(slug)
                    if rid:
                        await repo.update_health_status(rid, "down", str(exc)[:200], 1)
    except Exception as exc:
        log.error("[PIPELINE] Dynamic HTML pipeline failed: %s", exc)
    finally:
        await dynamic_engine.close()


async def run_tender_pipeline(repo, settings: Settings, dev_mode: bool, resource_map):
    from financial_scraper.fetchers.strategies import DynamicFetchStrategy
    from financial_scraper.scrapers.html_dynamic.factory import DynamicScraperFactory
    from financial_scraper.scrapers.engine import IntelligentEngine
    from financial_scraper.utils.dedup import UrlDeduplicator

    tender_strategy = DynamicFetchStrategy(headless=settings.playwright_headless)
    tender_engine = IntelligentEngine(
        strategy=tender_strategy,
        repository=repo,
        dedup=UrlDeduplicator(),
        rate_limit=settings.rate_limit_dynamic,
        dev_mode=dev_mode,
        resource_map=resource_map,
    )
    try:
        scraper = DynamicScraperFactory.get("ompic")
        results = await scraper.scrape(tender_strategy)
        for item in results:
            target = item.get("_target_table", "public_tender")
            data = {k: v for k, v in item.items() if k != "_target_table"}
            await tender_engine.save_result(target, data)
            await asyncio.sleep(settings.rate_limit_dynamic)
        await tender_engine.update_health("ompic", "healthy")
    except Exception as exc:
        log.error("[ompic] Tender scrape error: %s", exc)
        if repo and resource_map:
            rid = resource_map.get("ompic")
            if rid:
                await repo.update_health_status(rid, "down", str(exc)[:200], 1)
    finally:
        await tender_engine.close()


async def run_all_pipelines(repo, settings: Settings, resource_map: dict[str, str] | None = None) -> None:
    from financial_scraper.utils.cache import CheckpointStore

    log.info("[PIPELINE] Running all pipelines on startup")
    dev_mode = not settings.use_db

    checkpoint = CheckpointStore()

    # Phase 1: RSS + Dynamic in parallel (both take ~4-6 min each, together ~6 min)
    rss_task = asyncio.create_task(
        run_rss_pipeline(repo, settings, dev_mode, checkpoint, resource_map)
    )
    dynamic_task = asyncio.create_task(
        run_dynamic_pipeline(repo, settings, dev_mode, resource_map)
    )

    await asyncio.gather(rss_task, dynamic_task, return_exceptions=True)

    # Phase 2: Static HTML (short, no Playwright)
    await run_static_pipeline(repo, settings, dev_mode, checkpoint, resource_map)

    # Phase 3: Tender (OMPIC — separate, after dynamic is done)
    await run_tender_pipeline(repo, settings, dev_mode, resource_map)

    log.info("[PIPELINE] All initial pipelines complete")


async def register_scraping_resources(repo) -> dict[str, str]:
    """Register all active scraping sources in the scraping_resource table.
    Returns a dict mapping slug → resource_id for health tracking.
    """
    from financial_scraper.scrapers.rss.feeds import ACTIVE_FEEDS

    slug_to_rid: dict[str, str] = {}

    for feed in ACTIVE_FEEDS:
        rid = await repo.upsert_scraping_resource({
            "slug": feed["slug"],
            "name": feed.get("source_name", feed["slug"]),
            "base_url": feed["url"],
            "description": f"RSS feed: {feed.get('source_name', feed['slug'])}",
            "category": feed.get("target_table", "market"),
            "resource_type": feed.get("feed_type", "rss"),
            "is_active": feed.get("enabled", True),
        })
        slug_to_rid[feed["slug"]] = rid

    dynamic_sources = [
        ("bam", "Bank Al-Maghrib", "https://www.bkam.ma", "alert", "html_dynamic"),
        ("ammc", "AMMC", "https://www.ammc.ma", "alert", "html_dynamic"),
        ("acaps", "ACAPS", "https://www.acaps.ma", "alert", "html_dynamic"),
        ("anrt", "ANRT", "https://www.anrt.ma", "alert", "html_dynamic"),
        ("mef", "MEF", "https://www.finances.gov.ma", "alert", "html_dynamic"),
        ("cgem", "CGEM", "https://www.cgem.ma", "market", "html_dynamic"),
        ("worldbank", "World Bank", "https://www.worldbank.org", "market", "html_dynamic"),
        ("afdb", "AfDB", "https://www.afdb.org", "market", "html_dynamic"),
        ("ebrd", "EBRD", "https://www.ebrd.com", "market", "html_dynamic"),
        ("ompic", "OMPIC", "https://www.directompic.ma", "public_tender", "html_dynamic"),
    ]

    for slug, name, base_url, category, rtype in dynamic_sources:
        rid = await repo.upsert_scraping_resource({
            "slug": slug,
            "name": name,
            "base_url": base_url,
            "description": f"Dynamic scraper: {name}",
            "category": category,
            "resource_type": rtype,
            "is_active": True,
        })
        slug_to_rid[slug] = rid

    return slug_to_rid


async def main():
    configure_logging(cfg.log_level)
    log.info("[PIPELINE] Financial Scraper starting (USE_DB=%s)", cfg.use_db)

    repo = None
    resource_map = None
    if cfg.use_db:
        from financial_scraper.db.repository import PostgreSQLRepository
        repo = PostgreSQLRepository(cfg.database_url)
        await repo.connect()
        resource_map = await register_scraping_resources(repo)
        log.info("[PIPELINE] Scraping resources registered in DB")

    await run_all_pipelines(repo, cfg, resource_map)

    from financial_scraper.scheduler.jobs import start_scheduler
    start_scheduler(repo, cfg)

    try:
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        log.info("[PIPELINE] Shutting down...")
    finally:
        if repo:
            await repo.close()


if __name__ == "__main__":
    asyncio.run(main())
