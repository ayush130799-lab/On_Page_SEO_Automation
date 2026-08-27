"""JavaScript rendering via Playwright — a deliberate fallback, never the default path.

Rendering costs roughly 50× an HTTP fetch, so it runs only when the static HTML is demonstrably
insufficient (see :func:`needs_rendering`) and is bounded by both a concurrency semaphore and a
per-run page budget.
"""

from __future__ import annotations

import asyncio
import logging
import re

from ...config import settings

logger = logging.getLogger(__name__)

#: Root elements that frameworks mount into or framework hydration scripts.
_SPA_ROOTS = re.compile(
    r'<(?:div|main|section)[^>]+id=["\'](?:root|app|__next|__nuxt|q-app|svelte)["\'][^>]*>'
    r'|__NEXT_DATA__|self\.__next_f|window\.__NUXT__|window\.__INITIAL_STATE__',
    re.IGNORECASE,
)
_BODY_TEXT = re.compile(r"<body[^>]*>(.*?)</body>", re.IGNORECASE | re.DOTALL)
_TAGS = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>|<[^>]+>", re.IGNORECASE | re.DOTALL)


def needs_rendering(html: str, *, render_mode: str = "auto", min_text_length: int | None = None) -> bool:
    """Decide whether a page should be re-fetched through a browser.

    ``always`` and ``never`` short-circuit; ``auto`` looks for an SPA mount point or a body
    whose visible text is below the configured threshold.
    """
    if render_mode == "never":
        return False
    if render_mode == "always":
        return True

    if not html:
        return True

    if _SPA_ROOTS.search(html):
        return True

    threshold = settings.render_min_text_length if min_text_length is None else min_text_length
    body_match = _BODY_TEXT.search(html)
    body = body_match.group(1) if body_match else html
    visible = _TAGS.sub(" ", body)
    return len(re.sub(r"\s+", " ", visible).strip()) < threshold


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
                    args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
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
                    viewport={"width": 1366, "height": 900},
                    ignore_https_errors=False,
                )
                page = await context.new_page()
                # Images and fonts do not affect extraction; skipping them is a large speed win.
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
                    await page.wait_for_load_state("load", timeout=3000)
                except Exception:
                    pass

                # Give client-side frameworks (React/Next.js/Vue) 1s to mount DOM nodes
                await page.wait_for_timeout(1000)

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
