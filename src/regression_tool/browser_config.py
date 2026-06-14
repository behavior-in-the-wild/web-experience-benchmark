from __future__ import annotations

import os
from typing import Any


VIEWPORT_WIDTH = int(os.getenv("REGRESSION_VIEWPORT_WIDTH", "1280"))
VIEWPORT_HEIGHT = int(os.getenv("REGRESSION_VIEWPORT_HEIGHT", "900"))
DEVICE_SCALE_FACTOR = float(os.getenv("REGRESSION_DEVICE_SCALE_FACTOR", "1"))
LOCALE = os.getenv("REGRESSION_LOCALE", "en-US")
TIMEZONE_ID = os.getenv("REGRESSION_TIMEZONE", "UTC")
COLOR_SCHEME = os.getenv("REGRESSION_COLOR_SCHEME", "light")
REDUCED_MOTION = os.getenv("REGRESSION_REDUCED_MOTION", "reduce")
USER_AGENT = os.getenv(
    "REGRESSION_USER_AGENT",
    (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
)
GOTO_TIMEOUT_MS = int(os.getenv("REGRESSION_GOTO_TIMEOUT_MS", "30000"))
CONTENT_TIMEOUT_MS = int(os.getenv("REGRESSION_CONTENT_TIMEOUT_MS", "20000"))
NETWORK_SETTLE_TIMEOUT_MS = int(os.getenv("REGRESSION_NETWORK_SETTLE_TIMEOUT_MS", "5000"))
FONT_SETTLE_TIMEOUT_MS = int(os.getenv("REGRESSION_FONT_SETTLE_TIMEOUT_MS", "5000"))
EXTRA_SETTLE_MS = int(os.getenv("REGRESSION_EXTRA_SETTLE_MS", "500"))


def launch_kwargs() -> dict[str, Any]:
    return {
        "headless": True,
        "args": [
            "--disable-background-networking",
            "--disable-background-timer-throttling",
            "--disable-breakpad",
            "--disable-client-side-phishing-detection",
            "--disable-component-update",
            "--disable-default-apps",
            "--disable-dev-shm-usage",
            "--disable-extensions",
            "--disable-features=Translate,BackForwardCache",
            "--disable-renderer-backgrounding",
            "--disable-setuid-sandbox",
            "--metrics-recording-only",
            "--no-default-browser-check",
            "--no-first-run",
            "--no-sandbox",
        ],
    }


def context_kwargs() -> dict[str, Any]:
    return {
        "viewport": {"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
        "device_scale_factor": DEVICE_SCALE_FACTOR,
        "locale": LOCALE,
        "timezone_id": TIMEZONE_ID,
        "color_scheme": COLOR_SCHEME,
        "reduced_motion": REDUCED_MOTION,
        "user_agent": USER_AGENT,
        "ignore_https_errors": True,
    }


def snapshot_metadata() -> dict[str, Any]:
    return {
        "viewport": {"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
        "device_scale_factor": DEVICE_SCALE_FACTOR,
        "locale": LOCALE,
        "timezone_id": TIMEZONE_ID,
        "color_scheme": COLOR_SCHEME,
        "reduced_motion": REDUCED_MOTION,
        "user_agent": USER_AGENT,
        "goto_timeout_ms": GOTO_TIMEOUT_MS,
        "content_timeout_ms": CONTENT_TIMEOUT_MS,
        "network_settle_timeout_ms": NETWORK_SETTLE_TIMEOUT_MS,
        "font_settle_timeout_ms": FONT_SETTLE_TIMEOUT_MS,
        "extra_settle_ms": EXTRA_SETTLE_MS,
    }


def new_context(browser):
    return browser.new_context(**context_kwargs())


async def new_async_context(browser):
    return await browser.new_context(**context_kwargs())


def launch_chromium(playwright):
    return playwright.chromium.launch(**launch_kwargs())


async def launch_chromium_async(playwright):
    return await playwright.chromium.launch(**launch_kwargs())


def goto_and_settle(page, url: str) -> None:
    try:
        page.goto(url, wait_until="load", timeout=GOTO_TIMEOUT_MS)
    except Exception:
        # Fall back to domcontentloaded when external resources prevent "load"
        page.goto(url, wait_until="domcontentloaded", timeout=GOTO_TIMEOUT_MS)
    settle_page(page)


def set_content_and_settle(page, html_content: str) -> None:
    page.set_content(html_content, wait_until="domcontentloaded", timeout=CONTENT_TIMEOUT_MS)
    settle_page(page)


async def set_content_and_settle_async(page, html_content: str) -> None:
    await page.set_content(
        html_content,
        wait_until="domcontentloaded",
        timeout=CONTENT_TIMEOUT_MS,
    )
    await settle_page_async(page)


def settle_page(page) -> None:
    _force_lazy_images(page)
    _scroll_page(page)
    try:
        page.wait_for_load_state("networkidle", timeout=NETWORK_SETTLE_TIMEOUT_MS)
    except Exception:
        pass
    try:
        page.wait_for_function(
            "() => !document.fonts || document.fonts.status === 'loaded'",
            timeout=FONT_SETTLE_TIMEOUT_MS,
        )
    except Exception:
        pass
    if EXTRA_SETTLE_MS > 0:
        page.wait_for_timeout(EXTRA_SETTLE_MS)


async def settle_page_async(page) -> None:
    await _force_lazy_images_async(page)
    await _scroll_page_async(page)
    try:
        await page.wait_for_load_state("networkidle", timeout=NETWORK_SETTLE_TIMEOUT_MS)
    except Exception:
        pass
    try:
        await page.wait_for_function(
            "() => !document.fonts || document.fonts.status === 'loaded'",
            timeout=FONT_SETTLE_TIMEOUT_MS,
        )
    except Exception:
        pass
    if EXTRA_SETTLE_MS > 0:
        await page.wait_for_timeout(EXTRA_SETTLE_MS)


def _force_lazy_images(page) -> None:
    page.evaluate(_FORCE_LAZY_IMAGES_JS)


async def _force_lazy_images_async(page) -> None:
    await page.evaluate(_FORCE_LAZY_IMAGES_JS)


def _scroll_page(page) -> None:
    page.evaluate(_SCROLL_PAGE_JS)


async def _scroll_page_async(page) -> None:
    await page.evaluate(_SCROLL_PAGE_JS)


_FORCE_LAZY_IMAGES_JS = """() => {
    document.querySelectorAll('img[loading="lazy"]').forEach(img => {
        img.loading = 'eager';
        if (img.dataset && img.dataset.src && !img.src) img.src = img.dataset.src;
        if (img.dataset && img.dataset.srcset && !img.srcset) img.srcset = img.dataset.srcset;
    });
}"""


_SCROLL_PAGE_JS = """async () => {
    await new Promise(resolve => {
        let pos = 0;
        const step = Math.max(200, window.innerHeight || 900);
        const scroll = () => {
            pos += step;
            window.scrollTo(0, pos);
            if (pos < document.body.scrollHeight) {
                setTimeout(scroll, 80);
            } else {
                window.scrollTo(0, 0);
                setTimeout(resolve, 300);
            }
        };
        scroll();
    });
}"""
