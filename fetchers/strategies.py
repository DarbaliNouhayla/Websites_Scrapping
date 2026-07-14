import asyncio
import logging
from abc import ABC, abstractmethod

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

log = logging.getLogger("financial_scraper.fetchers.strategies")

# Full desktop Chrome 122 fingerprint — used by StaticFetchStrategy
HEADLESS_CHROME_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Accept-Language": "en-US,en;q=0.9,fr;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "max-age=0",
    "Sec-Ch-Ua": '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

HEADERS = {
    "User-Agent": HEADLESS_CHROME_HEADERS["User-Agent"],
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    "Accept": HEADLESS_CHROME_HEADERS["Accept"],
}


class FetchStrategy(ABC):
    @abstractmethod
    async def fetch(self, url: str) -> str:
        ...


class StaticFetchStrategy(FetchStrategy):
    def __init__(self):
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers=HEADERS,
                follow_redirects=True,
                timeout=httpx.Timeout(30.0),
            )
        return self._client

    @retry(
        retry=retry_if_exception_type((httpx.HTTPError, TimeoutError, asyncio.TimeoutError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        reraise=True,
    )
    async def fetch(self, url: str) -> str:
        client = await self._get_client()
        log.debug("[static] Fetching %s", url)
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.text

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None


class DynamicFetchStrategy(FetchStrategy):
    def __init__(self, headless: bool = True):
        self._headless = headless
        self._browser = None
        self._playwright = None

    async def _ensure_browser(self):
        if self._browser is None:
            from playwright.async_api import async_playwright
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=self._headless,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-web-security",
                    "--disable-features=IsolateOrigins,site-per-process",
                    "--ignore-certificate-errors",
                ],
            )
            log.info("[dynamic] Browser launched (headless=%s)", self._headless)
        return self._browser

    @retry(
        retry=retry_if_exception_type((TimeoutError, Exception)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        reraise=True,
    )
    async def fetch(
        self,
        url: str,
        wait_selector: str | None = None,
        timeout_ms: int | None = None,
        wait_until: str | None = None,
    ) -> str:
        browser = await self._ensure_browser()
        context = await browser.new_context(
            locale="fr-FR",
            viewport={"width": 1280, "height": 720},
            timezone_id="Africa/Casablanca",
            ignore_https_errors=True,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()
        # Comprehensive stealth init — masks headless detection for Cloudflare et al.
        await page.add_init_script("""
            // Mask webdriver property
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

            // Realistic plugin count (headless returns 0)
            Object.defineProperty(navigator, 'plugins', {
                get: () => [1, 2, 3, 4, 5]
            });

            // Realistic language set
            Object.defineProperty(navigator, 'languages', {
                get: () => ['fr-FR', 'fr', 'en']
            });

            // Chrome runtime (headless lacks this)
            window.chrome = {
                runtime: {},
                loadTimes: function() {},
                csi: function() {},
                app: {}
            };

            // WebGL vendor — headless reports "Google Inc."
            const getParameter = WebGLRenderingContext.prototype.getParameter;
            WebGLRenderingContext.prototype.getParameter = function(parameter) {
                if (parameter === 37445) return 'Intel Inc.';
                if (parameter === 37446) return 'Intel Iris OpenGL Engine';
                return getParameter(parameter);
            };

            // Override permissions query to hide headless
            const originalQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (parameters) => (
                parameters.name === 'notifications' ?
                    Promise.resolve({ state: Notification.permission }) :
                    originalQuery(parameters)
            );
        """)
        try:
            nav_timeout = timeout_ms or 35_000
            # Use the provided wait_until strategy, default to domcontentloaded
            wait_strategy = wait_until or "domcontentloaded"
            await page.goto(url, wait_until=wait_strategy, timeout=nav_timeout)
            # Only attempt networkidle if not explicitly set to something else
            if wait_strategy == "domcontentloaded":
                try:
                    await page.wait_for_load_state("networkidle", timeout=min(8_000, nav_timeout))
                except Exception:
                    pass
            if wait_selector:
                sel_timeout = max(3_000, min(15_000, nav_timeout // 2))
                try:
                    await page.wait_for_selector(wait_selector, state="attached", timeout=sel_timeout)
                    log.info("[dynamic] wait_selector '%s' matched on %s", wait_selector, url)
                except Exception as exc:
                    log.debug("[dynamic] wait_selector '%s' not found on %s: %s", wait_selector, url, exc)
            await page.wait_for_timeout(1500)
            content = await page.content()
            log.info("[dynamic] fetched %s: %d bytes", url, len(content))
            return content
        except Exception as e:
            log.warning("[dynamic] fetch failed for %s: %s", url, e)
            raise
        finally:
            await context.close()

    async def close(self) -> None:
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
            log.info("[dynamic] Browser closed")
