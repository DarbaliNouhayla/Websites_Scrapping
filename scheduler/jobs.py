import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from financial_scraper.config.settings import settings
from financial_scraper.scrapers.rss.runner import RSSPipeline
from financial_scraper.utils.dedup import UrlDeduplicator

log = logging.getLogger("financial_scraper.scheduler.jobs")

_repo = None
_settings = settings


def set_repository(repo):
    global _repo
    _repo = repo


async def rss_job() -> None:
    log.info("[PIPELINE] Starting RSS scheduled job")
    try:
        pipeline = RSSPipeline(
            repository=_repo,
            dedup=UrlDeduplicator(),
            dev_mode=not _settings.use_db,
        )
        await pipeline.run()
        await pipeline.close()
    except Exception as exc:
        log.error("[PIPELINE] RSS job failed: %s", exc)


async def media_job() -> None:
    from financial_scraper.fetchers.strategies import StaticFetchStrategy
    from financial_scraper.scrapers.html_static.factory import HTMLScraperFactory
    from financial_scraper.scrapers.engine import IntelligentEngine

    log.info("[PIPELINE] Starting HTML static (media) scheduled job")
    slugs = ["medias24"]
    try:
        strategy = StaticFetchStrategy()
        engine = IntelligentEngine(
            strategy=strategy,
            repository=_repo,
            dedup=UrlDeduplicator(),
            rate_limit=_settings.rate_limit_static,
            dev_mode=not _settings.use_db,
        )
        for slug in slugs:
            try:
                scraper = HTMLScraperFactory.get(slug)
                results = await scraper.scrape(strategy)
                for item in results:
                    target = item.get("_target_table", "market")
                    data = {k: v for k, v in item.items() if k != "_target_table"}
                    await engine.save_result(target, data)
                    await asyncio.sleep(_settings.rate_limit_static)
            except Exception as exc:
                log.error("[%s] Static scrape failed: %s", slug, exc)
        await engine.close()
    except Exception as exc:
        log.error("[PIPELINE] Media job failed: %s", exc)
    log.info("[PIPELINE] HTML static job complete")


async def regulatory_job() -> None:
    from financial_scraper.fetchers.strategies import DynamicFetchStrategy
    from financial_scraper.scrapers.html_dynamic.factory import DynamicScraperFactory
    from financial_scraper.scrapers.engine import IntelligentEngine

    log.info("[PIPELINE] Starting HTML dynamic (regulatory) scheduled job")
    slugs = ["bam", "ammc", "acaps", "mef", "anrt", "cgem", "worldbank", "afdb", "ebrd"]
    try:
        strategy = DynamicFetchStrategy(headless=_settings.playwright_headless)
        engine = IntelligentEngine(
            strategy=strategy,
            repository=_repo,
            dedup=UrlDeduplicator(),
            rate_limit=_settings.rate_limit_dynamic,
            dev_mode=not _settings.use_db,
        )
        for slug in slugs:
            try:
                scraper = DynamicScraperFactory.get(slug)
                results = await scraper.scrape(strategy)
                for item in results:
                    target = item.get("_target_table", "alert")
                    data = {k: v for k, v in item.items() if k != "_target_table"}
                    await engine.save_result(target, data)
                    await asyncio.sleep(_settings.rate_limit_dynamic)
            except Exception as exc:
                log.error("[%s] Dynamic scrape failed: %s", slug, exc)
        await engine.close()
    except Exception as exc:
        log.error("[PIPELINE] Regulatory job failed: %s", exc)
    log.info("[PIPELINE] HTML dynamic job complete")


async def tender_job() -> None:
    from financial_scraper.fetchers.strategies import DynamicFetchStrategy
    from financial_scraper.scrapers.html_dynamic.factory import DynamicScraperFactory
    from financial_scraper.scrapers.engine import IntelligentEngine

    log.info("[PIPELINE] Starting tender scheduled job")
    try:
        strategy = DynamicFetchStrategy(headless=_settings.playwright_headless)
        engine = IntelligentEngine(
            strategy=strategy,
            repository=_repo,
            dedup=UrlDeduplicator(),
            rate_limit=_settings.rate_limit_dynamic,
            dev_mode=not _settings.use_db,
        )
        try:
            scraper = DynamicScraperFactory.get("ompic")
            results = await scraper.scrape(strategy)
            for item in results:
                target = item.get("_target_table", "public_tender")
                data = {k: v for k, v in item.items() if k != "_target_table"}
                await engine.save_result(target, data)
                await asyncio.sleep(_settings.rate_limit_dynamic)
        except Exception as exc:
            log.error("[ompic] Tender scrape failed: %s", exc)
        await engine.close()
    except Exception as exc:
        log.error("[PIPELINE] Tender job failed: %s", exc)
    log.info("[PIPELINE] Tender job complete")


def start_scheduler(repo, s) -> AsyncIOScheduler:
    set_repository(repo)
    scheduler = AsyncIOScheduler()
    scheduler.add_job(rss_job, "interval", minutes=s.rss_interval_minutes, id="rss_pipeline")
    scheduler.add_job(media_job, "interval", minutes=s.html_static_interval_minutes, id="html_static_pipeline")
    scheduler.add_job(regulatory_job, "interval", hours=s.html_dynamic_interval_hours, id="html_dynamic_pipeline")
    scheduler.add_job(tender_job, "interval", hours=s.tender_interval_hours, id="tender_pipeline")
    scheduler.start()
    log.info(
        "Scheduler started — RSS:%dmin Media:%dmin Regulatory:%dh Tender:%dh",
        s.rss_interval_minutes, s.html_static_interval_minutes,
        s.html_dynamic_interval_hours, s.tender_interval_hours,
    )
    return scheduler
