"""
Playwright-based crawler with stealth settings, user-agent rotation,
random delays, and retry logic.
"""

import asyncio
import logging
import random
import time
from typing import Optional

from playwright.async_api import async_playwright, Page, BrowserContext, TimeoutError as PWTimeout

logger = logging.getLogger(__name__)

# ── User-agent pool ───────────────────────────────────────────────────────────
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_3_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3.1 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36 Edg/121.0.0.0",
]

VIEWPORTS = [
    {"width": 1920, "height": 1080},
    {"width": 1440, "height": 900},
    {"width": 1366, "height": 768},
    {"width": 1280, "height": 800},
]


def _stealth_init_script() -> str:
    """JS injected into every page to mask automation fingerprints."""
    return """
    // Overwrite the `navigator.webdriver` property
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

    // Fake plugins array
    Object.defineProperty(navigator, 'plugins', {
        get: () => [1, 2, 3, 4, 5],
    });

    // Fake language
    Object.defineProperty(navigator, 'languages', {
        get: () => ['en-US', 'en'],
    });

    // Remove __playwright from window
    delete window.__playwright;
    delete window.__pw_manual;
    delete window.__PW_inspect;

    // Spoof chrome object
    window.chrome = {
        runtime: {},
        loadTimes: function() {},
        csi: function() {},
        app: {},
    };

    // Fix permissions query
    const originalQuery = window.navigator.permissions.query;
    window.navigator.permissions.query = (parameters) =>
        parameters.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission })
            : originalQuery(parameters);
    """


async def _build_context(playwright, user_agent: str, viewport: dict) -> BrowserContext:
    browser = await playwright.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
            "--disable-dev-shm-usage",
            "--disable-extensions",
            "--no-first-run",
            "--ignore-certificate-errors",
        ],
    )
    context = await browser.new_context(
        user_agent=user_agent,
        viewport=viewport,
        locale="en-GB",
        timezone_id="Europe/London",
        java_script_enabled=True,
        accept_downloads=False,
        extra_http_headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-GB,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
        },
    )
    await context.add_init_script(_stealth_init_script())
    return context


def _is_blocked(html: str, status: int) -> bool:
    """Heuristics to detect a bot-block / CAPTCHA response."""
    if status in (403, 429, 503):
        return True
    lower = html.lower()
    block_signals = [
        "captcha",
        "access denied",
        "blocked",
        "cloudflare",
        "please verify you are human",
        "enable javascript and cookies",
        "checking your browser",
        "ddos-guard",
        "bot detected",
    ]
    return any(s in lower for s in block_signals)


async def fetch_page(
    url: str,
    delay_min: float = 2.0,
    delay_max: float = 5.0,
    max_retries: int = 3,
    retry_wait: int = 60,
) -> Optional[str]:
    """
    Fetch a URL using Playwright with stealth settings.

    Returns the raw HTML string, or None if all retries are exhausted.
    """
    attempt = 0
    while attempt < max_retries:
        ua = random.choice(USER_AGENTS)
        vp = random.choice(VIEWPORTS)

        logger.info("Fetching %s (attempt %d/%d, UA: ...%s)", url, attempt + 1, max_retries, ua[-30:])

        async with async_playwright() as pw:
            context = await _build_context(pw, ua, vp)
            page: Page = await context.new_page()

            try:
                # Random pre-request delay
                await asyncio.sleep(random.uniform(delay_min, delay_max))

                response = await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=30_000,
                )

                # Small pause to let JS settle
                await asyncio.sleep(random.uniform(1.0, 2.5))

                status = response.status if response else 0
                html = await page.content()

                if _is_blocked(html, status):
                    logger.warning(
                        "Blocked on %s (status=%d). Waiting %ds before retry.",
                        url, status, retry_wait,
                    )
                    await context.close()
                    attempt += 1
                    if attempt < max_retries:
                        time.sleep(retry_wait)
                    continue

                await context.close()
                logger.info("Successfully fetched %s (%d chars)", url, len(html))
                return html

            except PWTimeout:
                logger.warning("Timeout on %s (attempt %d)", url, attempt + 1)
                await context.close()
                attempt += 1
                if attempt < max_retries:
                    time.sleep(retry_wait)

            except Exception as exc:
                logger.error("Error fetching %s: %s", url, exc)
                await context.close()
                attempt += 1
                if attempt < max_retries:
                    time.sleep(retry_wait)

    logger.error("All %d attempts exhausted for %s", max_retries, url)
    return None


async def crawl_urls(
    urls: list[str],
    delay_min: float = 2.0,
    delay_max: float = 5.0,
    max_retries: int = 3,
    retry_wait: int = 60,
) -> dict[str, Optional[str]]:
    """
    Crawl a list of URLs sequentially (to respect rate limits).
    Returns {url: html_or_None}.
    """
    results = {}
    for url in urls:
        html = await fetch_page(
            url,
            delay_min=delay_min,
            delay_max=delay_max,
            max_retries=max_retries,
            retry_wait=retry_wait,
        )
        results[url] = html
        # Inter-request pause
        await asyncio.sleep(random.uniform(delay_min, delay_max))
    return results
