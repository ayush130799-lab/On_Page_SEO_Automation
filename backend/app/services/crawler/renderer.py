"""JavaScript rendering via Playwright — a deliberate fallback, never the default path.

Rendering costs roughly 50× an HTTP fetch, so it runs only when the static HTML is demonstrably
insufficient (see :func:`needs_rendering`) and is bounded by both a concurrency semaphore and a
per-run page budget.
"""

from __future__ import annotations

import asyncio
import logging
import re

from bs4 import BeautifulSoup

from ...config import settings

logger = logging.getLogger(__name__)

#: Mirrors extractor._NON_RENDERING_TAGS / _CHROME_TAGS: elements that never render as prose, and
#: site chrome that is not "the content of the page". Kept as a local copy rather than imported —
#: those are private to extractor.py — but the scope must agree with what extract_page() will
#: measure once the page is actually parsed, or this check and the real word count judge two
#: different things again.
_NON_RENDERING_TAGS = ("script", "style", "template", "svg", "iframe", "noscript")
_CHROME_TAGS = ("nav", "header", "footer", "aside", "form", "figcaption", "dialog")


def _main_content_length(html: str) -> int:
    """Character length of the page's main content, with chrome and non-rendering tags removed.

    Measuring the *whole* body (as this used to) let nav menus, footers, cookie banners and
    sidebars — none of which are "the content of the page" — count toward "this page already has
    enough text, skip rendering". A page can easily clear 400 characters of boilerplate chrome
    while its actual `<main>`/`<article>` body is still an empty shell waiting on client-side
    hydration, which is precisely the page this check exists to catch. Scoping to the same region
    extract_page() treats as content means a page that would be reported as thin *after*
    extraction is also recognised as thin *before* deciding whether to render it.
    """
    if not html:
        return 0
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        return 0

    body = soup.body or soup
    container = body.find("main") or body.find("article") or body
    for tag in container.find_all(_NON_RENDERING_TAGS + _CHROME_TAGS):
        tag.decompose()
    text = container.get_text(" ", strip=True)
    return len(re.sub(r"\s+", " ", text).strip())


def needs_rendering(html: str, *, render_mode: str = "auto", min_text_length: int | None = None) -> bool:
    """Decide whether a page should be re-fetched through a browser.

    ``always`` and ``never`` short-circuit; ``auto`` renders when the page's main content
    (the same scope :mod:`extractor` measures — ``<main>``/``<article>`` with chrome stripped)
    is below the configured character threshold.
    """
    if render_mode == "never":
        return False
    if render_mode == "always":
        return True

    if not html:
        return True

    threshold = settings.render_min_text_length if min_text_length is None else min_text_length
    return _main_content_length(html) < threshold


class PlaywrightRenderer:
    """Owns one Chromium instance for the lifetime of a crawl.

    Launching a browser takes seconds, so it is started lazily on first use and shared by every
    worker through a semaphore rather than started per page.
    """

    def __init__(self, concurrency: int | None = None, timeout_ms: int | None = None):
        self._concurrency = concurrency or settings.render_concurrency
        self._timeout_ms = timeout_ms or settings.render_timeout_ms
        self._semaphore = asyncio.Semaphore(self._concurrency)
        self._playwright = None
        self._browser = None
        self._start_lock = asyncio.Lock()
        self._unavailable = False
        self.rendered_count = 0

    @property
    def available(self) -> bool:
        return not self._unavailable

    async def _ensure_browser(self) -> bool:
        if self._browser is not None:
            return True
        if self._unavailable:
            return False

        async with self._start_lock:
            if self._browser is not None:
                return True
            if self._unavailable:
                return False
            try:
                from playwright.async_api import async_playwright

                self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.launch(
                    headless=True,
                    args=[
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-gpu",
                        "--disable-setuid-sandbox",
                        "--no-zygote",
                        "--js-flags=--max-old-space-size=128",
                    ],
                )
                logger.info("Playwright renderer started (concurrency=%d).", self._concurrency)
                return True
            except Exception as exc:
                # A missing browser binary must degrade the crawl, not fail it.
                logger.warning(
                    "JavaScript rendering unavailable (%s). "
                    "Run 'playwright install chromium' to enable it.",
                    exc,
                )
                self._unavailable = True
                return False

    async def render(self, url: str, user_agent: str | None = None) -> str | None:
        """Return the DOM after rendering JS, or ``None`` if rendering was not possible."""
        if not await self._ensure_browser():
            return None

        async with self._semaphore:
            context = None
            page = None
            try:
                context = await self._browser.new_context(
                    user_agent=user_agent or settings.user_agent,
                    viewport={"width": 1920, "height": 1080},
                    ignore_https_errors=False,
                    locale="en-US",
                )
                page = await context.new_page()
                # Images and media do not affect SEO text/link extraction; skipping them speeds up rendering.
                await page.route(
                    "**/*",
                    lambda route: asyncio.ensure_future(
                        route.abort()
                        if route.request.resource_type in {"image", "font", "media"}
                        else route.continue_()
                    ),
                )
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=self._timeout_ms)
                except Exception as goto_exc:
                    logger.debug("goto domcontentloaded timeout/error for %s: %s", url, goto_exc)

                try:
                    await page.wait_for_load_state("load", timeout=2000)
                except Exception:
                    pass

                # JetOctopus-grade networkidle wait: let client-side router & API calls resolve
                try:
                    await page.wait_for_load_state("networkidle", timeout=2500)
                except Exception:
                    pass

                # If an SPA root container exists, wait for dynamic child nodes to mount
                try:
                    await page.wait_for_selector(
                        "#root > *:not(#seo-fallback), #app > *, #__next > *, main",
                        timeout=2000,
                    )
                except Exception:
                    pass

                # Settle time for dynamic title / meta / hydration updates (React Helmet, Next Head)
                await page.wait_for_timeout(400)

                html = await page.content()
                if html:
                    self.rendered_count += 1
                    return html
                return None
            except Exception as exc:
                if page is not None:
                    try:
                        html = await page.content()
                        if html and len(html) > 200:
                            self.rendered_count += 1
                            return html
                    except Exception:
                        pass
                logger.warning("Rendering failed for %s: %s", url, exc)
                return None
            finally:
                if context is not None:
                    try:
                        await context.close()
                    except Exception:
                        pass

    async def close(self) -> None:
        for closer in (self._browser, self._playwright):
            if closer is None:
                continue
            try:
                await (closer.close() if closer is self._browser else closer.stop())
            except Exception:
                pass
        self._browser = None
        self._playwright = None
