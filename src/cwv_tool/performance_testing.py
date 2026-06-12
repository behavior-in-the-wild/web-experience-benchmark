from __future__ import annotations

import asyncio
import json
import os
import time
import statistics
from urllib.parse import urldefrag, urlparse
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from cwv_tool.logger import get_logger
from cwv_tool.utils import save_json_file
from cwv_tool.server_utils import (
    kill_server,
    start_framework_server,
    checkout_branch,
    get_default_branch,
)

logger = get_logger(__name__)

# Try to import playwright for CWV measurement
try:
    from playwright.async_api import async_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    logger.warning("Playwright not available for CWV measurement")

# ---------- Configuration ----------


"""Performance testing service for CWV metrics.

Properly hosts each branch and measures Core Web Vitals using Playwright.
Uses improved measurement approach with session-window CLS, real interaction-based INP tracking,
retry logic, and IQR-based outlier removal.
"""

"""
1. Decide SETTLE_TIME_CANDIDATES = []
2. Set --csv [path/to/csv] or load from hf
"""


DEFAULT_TIMEOUT = 120000  # 120s navigation timeout
DEFAULT_WAIT_STRATEGY = "domcontentloaded"  # More reliable than networkidle
# DEFAULT_SETTLE_TIME = 5000  # ms to wait for page to stabilize (increase to allow resources to load)
MAX_RETRIES = 1

SETTLE_TIME_CANDIDATES = [5000, 10000]
# SETTLE_TIME_CANDIDATES = [5000] # only trying with 5000ms i.e. 5s and not retrying with 10s
DEFAULT_SETTLE_TIME = SETTLE_TIME_CANDIDATES[0]

BROWSER_LAUNCH_ARGS = [
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-extensions",
    "--disable-default-apps",
    "--disable-sync",
    "--disable-component-update",
    "--disable-features=Translate,MediaRouter",
    "--no-first-run",
    "--no-default-browser-check",
]
if os.getenv("CWV_DOCKER_BROWSER", "0").strip().lower() in {"1", "true", "yes"}:
    BROWSER_LAUNCH_ARGS.append("--disable-dev-shm-usage")


# Rating thresholds (based on Google's CWV thresholds)
THRESHOLDS = {
    'lcp': {'good': 2500, 'needs_improvement': 4000},  # ms
    'cls': {'good': 0.1, 'needs_improvement': 0.25},
    'fid': {'good': 100, 'needs_improvement': 300},  # ms
    'inp': {'good': 200, 'needs_improvement': 500},  # ms
    'ttfb': {'good': 800, 'needs_improvement': 1800},  # ms
}


def get_measurement_config(
    device: str,
    wait_strategy: str = DEFAULT_WAIT_STRATEGY,
    settle_time: int = DEFAULT_SETTLE_TIME,
    simulate_interaction: bool = True,
    prevent_navigation_on_interaction: bool = True,
) -> Dict[str, Any]:
    config = DEVICE_CONFIGS.get(device, DEVICE_CONFIGS["desktop"])
    return {
        "device": device,
        "device_config": json.loads(json.dumps(config)),
        "browser_launch_args": list(BROWSER_LAUNCH_ARGS),
        "context": {
            "viewport": config["viewport"],
            "device_scale_factor": config["device_scale_factor"],
            "is_mobile": config["is_mobile"],
            "has_touch": config["has_touch"],
            "user_agent": config["user_agent"],
            "locale": "en-US",
            "timezone_id": "UTC",
            "color_scheme": "light",
            "reduced_motion": "no-preference",
            "ignore_https_errors": True,
            "extra_http_headers": {"Accept-Language": "en-US,en;q=0.9"},
        },
        "cdp": {
            "cache_disabled": True,
            "network_conditions": config["network_conditions"],
            "cpu_throttling": config["cpu_throttling"],
        },
        "wait_strategy": wait_strategy,
        "settle_time_ms": settle_time,
        "simulate_interaction": simulate_interaction,
        "prevent_navigation_on_interaction": prevent_navigation_on_interaction,
    }


def _summarize_network(url: str, requests: list[dict], failed: list[dict]) -> dict[str, Any]:
    page_host = urlparse(url).hostname or ""
    page_host = page_host.lower()
    domains: dict[str, int] = {}
    external_domains: dict[str, int] = {}
    third_party_count = 0
    for req in requests:
        host = (urlparse(req.get("url", "")).hostname or "").lower()
        if not host:
            continue
        domains[host] = domains.get(host, 0) + 1
        if page_host and host != page_host:
            third_party_count += 1
            external_domains[host] = external_domains.get(host, 0) + 1

    def top(items: dict[str, int]) -> list[dict[str, Any]]:
        return [
            {"host": host, "count": count}
            for host, count in sorted(items.items(), key=lambda item: (-item[1], item[0]))[:10]
        ]

    return {
        "request_count": len(requests),
        "failed_request_count": len(failed),
        "third_party_request_count": third_party_count,
        "top_domains": top(domains),
        "top_external_domains": top(external_domains),
        "failed_requests": failed[:20],
    }


async def _launch_browser(playwright, headless: bool):
    return await playwright.chromium.launch(headless=headless, args=BROWSER_LAUNCH_ARGS)

# Device-specific configurations for realistic testing
DEVICE_CONFIGS = {
    "desktop": {
        "viewport": {
            "width": 1500,
            "height": 800,
        },
        "device_scale_factor": 1,
        "is_mobile": False,
        "has_touch": False,
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        "cpu_throttling": 1,
        "network_conditions": {
            "offline": False,
            "latency": 0,
            "downloadThroughput": 10 * 1024 * 1024 / 8,  # 10 Mbps
            "uploadThroughput": 384 * 1024 / 8,  # 384 Kbps
        },
    },
    "mobile": {
        "viewport": {
            "width": 412,
            "height": 915,
        },
        "device_scale_factor": 2.625,
        "is_mobile": True,
        "has_touch": True,
        "user_agent": "Mozilla/5.0 (Linux; Android 12; Pixel 6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Mobile Safari/537.36",
        "cpu_throttling": 20,
        "network_conditions": {
            "offline": False,
            "latency": 150,  # 150ms latency
            "downloadThroughput": 1 * 1024 * 1024 / 8,  # 1 Mbps (slow 3G)
            "uploadThroughput": 384 * 1024 / 8,  # 384 Kbps
        },
    },
}


# ---------- Helpers ----------

def remove_outliers(values: List[float]) -> List[float]:
    """Remove outliers using IQR method."""
    if len(values) < 4:
        return values

    q1, q3 = statistics.quantiles(values, n=4)[0], statistics.quantiles(values, n=4)[2]
    iqr = q3 - q1
    low, high = q1 - 1.5 * iqr, q3 + 1.5 * iqr

    return [v for v in values if low <= v <= high]


def get_rating(metric: str, value: float) -> str:
    """Get Good/Needs Improvement/Poor rating based on thresholds."""
    thresholds = THRESHOLDS.get(metric)
    if not thresholds:
        return "Unknown"
    
    if value <= thresholds['good']:
        return "Good"
    elif value <= thresholds['needs_improvement']:
        return "Needs Improvement"
    else:
        return "Poor"


def get_webvitals_script() -> str:
    """Return the JavaScript to inject for collecting Web Vitals."""
    return """
        window.__webVitals = { 
            lcp: null, 
            cls: 0, 
            fid: null,
            inp: null,
            ttfb: null,
            fcp: null,
            interactions: [],
            // Detailed CLS and INP attribution
            clsShifts: [],
            inpInteractions: [],
            lcpElement: null,
            lcpElements: []
        };

        // LCP - collect ALL LCP entries with explicit details (tag, id, class, size, timing, url)
        try {
            new PerformanceObserver((list) => {
                const entries = list.getEntries();
                for (const entry of entries) {
                    const el = entry.element;
                    const detail = {
                        tagName: el?.tagName || 'unknown',
                        id: (el?.id && el.id.trim()) ? el.id : null,
                        className: (el?.className && typeof el.className === 'string' && el.className.trim()) ? el.className.trim() : null,
                        size: entry.size ?? null,
                        renderTime: entry.renderTime ?? null,
                        loadTime: entry.loadTime ?? null,
                        startTime: entry.startTime ?? null,
                        url: (entry.url && entry.url.trim()) ? entry.url : null
                    };
                    if (el?.tagName === 'IMG') {
                        detail.naturalWidth = el.naturalWidth ?? null;
                        detail.naturalHeight = el.naturalHeight ?? null;
                        detail.currentSrc = (el.currentSrc && el.currentSrc.trim()) ? el.currentSrc : null;
                    }
                    if (el?.tagName === 'VIDEO') {
                        detail.poster = (el.poster && el.poster.trim()) ? el.poster : null;
                    }
                    window.__webVitals.lcpElements.push(detail);
                }
                const lastEntry = entries[entries.length - 1];
                window.__webVitals.lcp = lastEntry.renderTime || lastEntry.loadTime;
                window.__webVitals.lcpElement = lastEntry.element?.tagName || 'unknown';
            }).observe({ type: 'largest-contentful-paint', buffered: true });
        } catch (e) {
            console.log('LCP observer not supported');
        }

        // CLS with session window approach (proper CLS calculation)
        let clsValue = 0;
        let sessionValue = 0;
        let sessionEntries = [];
        
        try {
            new PerformanceObserver((list) => {
                for (const entry of list.getEntries()) {
                    if (!entry.hadRecentInput) {
                        const firstSessionEntry = sessionEntries[0];
                        const lastSessionEntry = sessionEntries[sessionEntries.length - 1];
                        
                        if (sessionValue &&
                            entry.startTime - lastSessionEntry.startTime < 1000 &&
                            entry.startTime - firstSessionEntry.startTime < 5000) {
                            sessionValue += entry.value;
                            sessionEntries.push(entry);
                        } else {
                            sessionValue = entry.value;
                            sessionEntries = [entry];
                        }
                        
                        if (sessionValue > clsValue) {
                            clsValue = sessionValue;
                            window.__webVitals.cls = clsValue;
                        }

                        // Capture detailed CLS shift attribution for this entry
                        try {
                            const sources = (entry.sources || []).map((src) => {
                                const el = src.node;
                                return {
                                    tagName: el?.tagName || 'unknown',
                                    id: (el?.id && el.id.trim()) ? el.id : null,
                                    className: (el?.className && typeof el.className === 'string' && el.className.trim()) ? el.className.trim() : null,
                                    previousRect: src.previousRect || null,
                                    currentRect: src.currentRect || null,
                                };
                            });
                            window.__webVitals.clsShifts.push({
                                value: entry.value,
                                startTime: entry.startTime ?? null,
                                sources,
                            });
                        } catch (e) {
                            // CLS attribution is best-effort; ignore individual failures
                        }
                    }
                }
            }).observe({ type: 'layout-shift', buffered: true });
        } catch (e) {
            console.log('CLS observer not supported');
        }

        // FID
        try {
            new PerformanceObserver((list) => {
                const firstInput = list.getEntries()[0];
                if (firstInput && window.__webVitals.fid === null) {
                    window.__webVitals.fid = firstInput.processingStart - firstInput.startTime;
                }
            }).observe({ type: 'first-input', buffered: true });
        } catch (e) {
            console.log('FID observer not supported');
        }

        // INP - proper tracking with P75
        const interactionMap = new Map();
        
        try {
            new PerformanceObserver((list) => {
                for (const entry of list.getEntries()) {
                    if (!entry.interactionId) continue;
                    
                    const existingEntry = interactionMap.get(entry.interactionId);
                    if (!existingEntry || entry.duration > existingEntry.duration) {
                        interactionMap.set(entry.interactionId, entry);
                    }
                }
                
                const interactions = Array.from(interactionMap.values());
                window.__webVitals.interactions = interactions.map(e => e.duration);

                // Capture detailed INP interaction information (timing + basic attribution)
                window.__webVitals.inpInteractions = interactions.map((e) => {
                    let tagName = null;
                    let id = null;
                    let className = null;
                    try {
                        // Some browsers may expose a target element on PerformanceEventTiming;
                        // if not available, these will stay null.
                        const el = e.target || null;
                        if (el) {
                            tagName = el.tagName || null;
                            id = (el.id && el.id.trim()) ? el.id : null;
                            className = (el.className && typeof el.className === 'string' && el.className.trim()) ? el.className.trim() : null;
                        }
                    } catch (_) {
                        // Best-effort element attribution only
                    }
                    return {
                        interactionId: e.interactionId ?? null,
                        name: e.name ?? null,
                        duration: e.duration ?? null,
                        startTime: e.startTime ?? null,
                        processingStart: e.processingStart ?? null,
                        target: {
                            tagName,
                            id,
                            className,
                        },
                    };
                });
                
                if (interactions.length > 0) {
                    // INP is the p75 interaction latency
                    interactions.sort((a, b) => b.duration - a.duration);
                    const idx = Math.min(Math.floor(interactions.length * 0.25), interactions.length - 1);
                    window.__webVitals.inp = interactions[idx].duration;
                }
            }).observe({
                type: 'event',
                buffered: true,
                durationThreshold: 16
            });
        } catch (e) {
            console.log('INP observer not supported');
        }

        // TTFB
        try {
            new PerformanceObserver((list) => {
                const [navEntry] = list.getEntries();
                window.__webVitals.ttfb = navEntry.responseStart;
            }).observe({ type: 'navigation', buffered: true });
        } catch (e) {
            console.log('Navigation observer not supported');
        }
        
        // FCP
        try {
            new PerformanceObserver((list) => {
                const entries = list.getEntries();
                for (const entry of entries) {
                    if (entry.name === 'first-contentful-paint') {
                        window.__webVitals.fcp = entry.startTime;
                    }
                }
            }).observe({ type: 'paint', buffered: true });
        } catch (e) {
            console.log('Paint observer not supported');
        }
    """


async def measure_cwv_metrics(
    url: str,
    device: str = "desktop",
    headless: bool = True,
    timeout: int = DEFAULT_TIMEOUT,
    wait_strategy: str = DEFAULT_WAIT_STRATEGY,
    settle_time: int = DEFAULT_SETTLE_TIME,
    simulate_interaction: bool = True,
    prevent_navigation_on_interaction: bool = True,
    browser: Any | None = None,
) -> Dict[str, Any]:
    """Measure Core Web Vitals for a URL using Playwright.
    
    Measures:
    - LCP: Largest Contentful Paint
    - CLS: Cumulative Layout Shift (session window approach)
    - FID: First Input Delay
    - INP: Interaction to Next Paint (P75 from real page interactions only)
    - TTFB: Time to First Byte
    - FCP: First Contentful Paint
    
    Args:
        url: URL to measure
        device: Device type (mobile/desktop)
        headless: Run browser headlessly
        timeout: Navigation timeout in ms
        wait_strategy: Wait strategy (domcontentloaded/networkidle/load)
        settle_time: Time to wait for page to stabilize in ms
        simulate_interaction: Whether to interact with real page elements for INP/FID
        prevent_navigation_on_interaction: Block navigation requests triggered by interactions
        
    Returns:
        Dict with LCP, CLS, FID, INP, TTFB, FCP values (INP may be 0 if no interactions)
    """
    if not PLAYWRIGHT_AVAILABLE:
        return {
            "status": "skipped",
            "message": "Playwright not available",
            "LCP": 0,
            "CLS": 0,
            "FID": 0,
            "INP": 0,
            "TTFB": 0,
            "FCP": 0,
        }
    
    owns_browser = browser is None
    context = None
    try:
        async def _measure(active_browser):
            # Get device config (default to desktop if unknown)
            config = DEVICE_CONFIGS.get(device, DEVICE_CONFIGS["desktop"])
            measurement_config = get_measurement_config(
                device=device,
                wait_strategy=wait_strategy,
                settle_time=settle_time,
                simulate_interaction=simulate_interaction,
                prevent_navigation_on_interaction=prevent_navigation_on_interaction,
            )
            
            # Configure context with device-specific settings
            nonlocal context
            context = await active_browser.new_context(
                viewport=config["viewport"],
                device_scale_factor=config["device_scale_factor"],
                is_mobile=config["is_mobile"],
                has_touch=config["has_touch"],
                user_agent=config["user_agent"],
                locale="en-US",
                timezone_id="UTC",
                color_scheme="light",
                reduced_motion="no-preference",
                ignore_https_errors=True,
                extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
            )
            
            page = await context.new_page()
            request_log: list[dict[str, Any]] = []
            failed_request_log: list[dict[str, Any]] = []

            def on_request(request):
                request_log.append({
                    "url": request.url,
                    "method": request.method,
                    "resource_type": request.resource_type,
                })

            def on_request_failed(request):
                failure = request.failure or ""
                failed_request_log.append({
                    "url": request.url,
                    "method": request.method,
                    "resource_type": request.resource_type,
                    "failure": failure,
                })

            page.on("request", on_request)
            page.on("requestfailed", on_request_failed)
            
            # Apply CPU and network throttling via CDP
            cdp = await context.new_cdp_session(page)
            
            # Apply network conditions
            network_config = config["network_conditions"]
            await cdp.send('Network.enable')
            await cdp.send('Network.setCacheDisabled', {'cacheDisabled': True})
            await cdp.send('Network.emulateNetworkConditions', {
                'offline': network_config["offline"],
                'latency': network_config["latency"],
                'downloadThroughput': network_config["downloadThroughput"],
                'uploadThroughput': network_config["uploadThroughput"],
            })
            
            # Apply CPU throttling
            cpu_throttle = config["cpu_throttling"]
            if cpu_throttle > 1:
                await cdp.send('Emulation.setCPUThrottlingRate', {'rate': cpu_throttle})
            
            logger.debug(
                "Device config: %s (CPU: %dx, latency: %dms, download: %.1f Mbps)",
                device, cpu_throttle, network_config["latency"],
                network_config["downloadThroughput"] * 8 / 1024 / 1024
            )
            
            # Inject Performance Observer to capture metrics BEFORE navigation
            await page.add_init_script(get_webvitals_script())
            interaction_target = {"clicked": False, "selector": None}
            
            # Navigate with configurable wait strategy and timeout
            try:
                await page.goto(url, wait_until=wait_strategy, timeout=timeout)
            except Exception as nav_error:
                # If networkidle times out, try with domcontentloaded
                if "Timeout" in str(nav_error) and wait_strategy == "networkidle":
                    logger.warning("networkidle timeout, retrying with domcontentloaded")
                    await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
                else:
                    raise
            
            # Wait for page to stabilize
            await asyncio.sleep(settle_time / 1000)
            
            # Simulate realistic user interaction for FID/INP if enabled
            if simulate_interaction:
                # Prevent navigation by intercepting click events on links/forms
                if prevent_navigation_on_interaction:
                    await page.evaluate("""
                        window.__preventNavigation = (e) => {
                            if (e.target.tagName === 'A' || e.target.closest('a')) {
                                e.preventDefault();
                                e.stopPropagation();
                            }
                            if (e.target.tagName === 'FORM' || e.target.closest('form')) {
                                e.preventDefault();
                                e.stopPropagation();
                            }
                        };
                        window.__navBlocker = {
                            assign: window.location.assign.bind(window.location),
                            replace: window.location.replace.bind(window.location),
                            open: window.open,
                            pushState: history.pushState.bind(history),
                            replaceState: history.replaceState.bind(history),
                            beforeUnload: window.onbeforeunload,
                        };
                        window.location.assign = () => {};
                        window.location.replace = () => {};
                        window.open = () => null;
                        history.pushState = () => {};
                        history.replaceState = () => {};
                        window.onbeforeunload = (e) => {
                            e.preventDefault();
                            e.returnValue = '';
                            return '';
                        };
                        document.querySelectorAll('meta[http-equiv="refresh"]').forEach(m => m.remove());
                        document.addEventListener('click', window.__preventNavigation, true);
                        document.addEventListener('submit', window.__preventNavigation, true);
                    """)

                try:
                    # Try clicking real interactive elements on the page
                    # Priority: buttons > links > inputs (most common user interactions)
                    clicked = False
                    for selector in ['button:visible', 'a:visible', 'input:visible', '[role="button"]:visible']:
                        try:
                            element = page.locator(selector).first
                            if await element.is_visible(timeout=100):
                                interaction_target = await element.evaluate("""(el, selector) => ({
                                    selector,
                                    tagName: el.tagName || null,
                                    id: el.id || null,
                                    className: typeof el.className === 'string' ? el.className : null,
                                    text: (el.innerText || el.getAttribute('aria-label') || el.getAttribute('title') || '').trim().slice(0, 120)
                                })""", selector)
                                interaction_target["boundingBox"] = await element.bounding_box()
                                await element.click(timeout=500, force=True)
                                await asyncio.sleep(0.2)
                                clicked = True
                                logger.debug("Clicked real element: %s", selector)
                                break
                        except Exception as e:
                            logger.debug("Could not click %s: %s", selector, e)
                            continue
                    
                    if not clicked:
                        logger.debug("No clickable elements found on page")
                        interaction_target = {"selector": None, "clicked": False}
                    
                    # Simulate realistic scrolling behavior to observe CLS
                    # Many users scroll through pages naturally
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight / 4)")
                    await asyncio.sleep(0.3)
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
                    await asyncio.sleep(0.3)
                    await page.evaluate("window.scrollTo(0, 0)")
                    await asyncio.sleep(0.2)
                    
                except Exception as interact_error:
                    logger.debug("Interaction warning: %s", interact_error)
                finally:
                    if prevent_navigation_on_interaction:
                        await page.evaluate("""
                            if (window.__preventNavigation) {
                                document.removeEventListener('click', window.__preventNavigation, true);
                                document.removeEventListener('submit', window.__preventNavigation, true);
                                delete window.__preventNavigation;
                            }
                            if (window.__navBlocker) {
                                window.location.assign = window.__navBlocker.assign;
                                window.location.replace = window.__navBlocker.replace;
                                window.open = window.__navBlocker.open;
                                history.pushState = window.__navBlocker.pushState;
                                history.replaceState = window.__navBlocker.replaceState;
                                window.onbeforeunload = window.__navBlocker.beforeUnload;
                                delete window.__navBlocker;
                            }
                        """)
            
            # Final wait to collect all metrics
            await asyncio.sleep(1)
            
            # Get metrics
            metrics = await asyncio.wait_for(page.evaluate("() => window.__webVitals"), timeout=10)

            # time.sleep(60)
            browser_version = active_browser.version
            network_summary = _summarize_network(url, request_log, failed_request_log)
            
            return {
                "status": "success",
                "LCP": round(float(metrics.get("lcp") or 0), 4),
                "CLS": round(float(metrics.get("cls") or 0), 8),
                "FID": round(float(metrics.get("fid") or 0), 4),
                "INP": round(float(metrics.get("inp") or 0), 4),
                "TTFB": round(float(metrics.get("ttfb") or 0), 4),
                "FCP": round(float(metrics.get("fcp") or 0), 4),
                "lcp_element": metrics.get("lcpElement"),
                # "lcp_elements": metrics.get("lcpElements") or []
                # New detailed attribution fields
                "cls_shifts": metrics.get("clsShifts") or [],
                "inp_interactions": metrics.get("inpInteractions") or [],
                "interaction_target": interaction_target if simulate_interaction else {"clicked": False, "disabled": True},
                "interaction_steps": [
                    {"type": "click", "target": interaction_target if simulate_interaction else None},
                    {"type": "scroll", "to": "25%"},
                    {"type": "scroll", "to": "50%"},
                    {"type": "scroll", "to": "0%"},
                ] if simulate_interaction else [],
                "network_summary": network_summary,
                "measurement_config": measurement_config,
                "browser_version": browser_version,
            }

        if owns_browser:
            async with async_playwright() as p:
                browser = await _launch_browser(p, headless=headless)
                try:
                    return await _measure(browser)
                finally:
                    await browser.close()
        return await _measure(browser)
            
    except Exception as e:
        logger.error("Failed to measure CWV: %s", e)
        return {
            "status": "error",
            "error": str(e),
            "LCP": 0,
            "CLS": 0,
            "FID": 0,
            "INP": 0,
            "TTFB": 0,
            "FCP": 0,
        }
    finally:
        if context is not None:
            try:
                await context.close()
            except Exception:
                pass

async def measure_multiple_runs(
    url: str,
    device: str = "desktop",
    headless: bool = True,
    num_runs: int = 5,
    max_retries: int = MAX_RETRIES,
) -> tuple[List[Dict], int, bool]:
    """Measure CWV multiple times, keeping working settle_time across runs.
    
    Once a working settle_time is found (LCP > 0), it's reused for remaining runs.
    
    Args:
        url: URL to measure
        device: Device type
        headless: Run headlessly
        num_runs: Number of measurement runs
        max_retries: Maximum retry attempts per run
        
    Returns:
        Tuple of (list of run results, final settle_time used, success)
    """
    runs = []
    success = True
    current_settle_time = None

    def nan_run() -> Dict[str, Any]:
        return {
            "status": "error",
            "LCP": float("nan"),
            "CLS": float("nan"),
            "FID": float("nan"),
            "INP": float("nan"),
            "TTFB": float("nan"),
            "FCP": float("nan"),
            # "lcp_elements": [],
        }
    logger.info(f"{SETTLE_TIME_CANDIDATES = } in ms")
    async with async_playwright() as p:
        browser = await _launch_browser(p, headless=headless)
        try:
            for run_num in range(num_runs):
                logger.info("  Run %d/%d", run_num + 1, num_runs)

                metrics = None
                success_this_run = False

                candidate_times = (
                    [current_settle_time]
                    if current_settle_time is not None
                    else SETTLE_TIME_CANDIDATES
                )

                for settle_time in candidate_times:
                    logger.info("Current settle_time=%dms", settle_time)
                    metrics = await measure_cwv_metrics(
                        url,
                        device,
                        headless,
                        settle_time=settle_time,
                        browser=browser,
                    )

                    if metrics.get("status") == "success" and metrics.get("LCP", 0) > 0:
                        success_this_run = True
                        current_settle_time = settle_time
                        break

                    logger.info(
                        "    LCP=0 at settle_time=%dms, trying next (if any)",
                        settle_time,
                    )

                if not success_this_run:
                    logger.warning(
                        "    LCP failed at all settle_times for run %d; cancelling remaining runs and returning NaN",
                        run_num + 1,
                    )
                    runs.append(nan_run())
                    # Fill remaining runs with NaN and return (marked as SUCCESS, not FAILURE)
                    for _ in range(run_num + 1, num_runs):
                        runs.append(nan_run())
                    return runs, current_settle_time, True

                runs.append(metrics)
                await asyncio.sleep(0.5)
        finally:
            await browser.close()

    return runs, current_settle_time, success


def calculate_aggregated_metrics(runs: List[Dict]) -> Dict[str, Any]:
    """Calculate median, mean, and p75 metrics from multiple runs with outlier removal.
    
    Args:
        runs: List of metric dictionaries from individual runs
        
    Returns:
        Aggregated metrics with median, mean, stdev, p75
    """
    valid_runs = [r for r in runs if r.get("status") == "success"]
    
    if not valid_runs:
        return {
            "LCP_median": float("nan"), "LCP_mean": float("nan"), "LCP_p75": float("nan"),
            "CLS_median": float("nan"), "CLS_mean": float("nan"),
            "FID_median": float("nan"), "FID_mean": float("nan"),
            "INP_median": float("nan"), "INP_mean": float("nan"), "INP_p75": float("nan"),
            "TTFB_median": float("nan"), "TTFB_mean": float("nan"),
            "FCP_median": float("nan"), "FCP_mean": float("nan"),
            "valid_runs": 0, "total_runs": len(runs),
        }
    
    # Extract values with outlier removal
    lcp_raw = [r["LCP"] for r in valid_runs if r.get("LCP", 0) > 0]
    cls_raw = [r["CLS"] for r in valid_runs]
    fid_raw = [r["FID"] for r in valid_runs if r.get("FID", 0) > 0]
    inp_raw = [r["INP"] for r in valid_runs if r.get("INP", 0) > 0]
    ttfb_raw = [r["TTFB"] for r in valid_runs if r.get("TTFB", 0) > 0]
    fcp_raw = [r["FCP"] for r in valid_runs if r.get("FCP", 0) > 0]
    
    # Apply IQR outlier removal
    lcp_values = remove_outliers(lcp_raw) if lcp_raw else []
    cls_values = remove_outliers(cls_raw) if cls_raw else []
    fid_values = remove_outliers(fid_raw) if fid_raw else []
    inp_values = remove_outliers(inp_raw) if inp_raw else []
    ttfb_values = remove_outliers(ttfb_raw) if ttfb_raw else []
    fcp_values = remove_outliers(fcp_raw) if fcp_raw else []
    
    def calc_stats(values: List[float], decimals: int = 2) -> Dict[str, float]:
        if not values:
            return {"median": 0, "mean": 0, "stdev": 0, "p75": 0}
        return {
            "median": round(statistics.median(values), decimals),
            "mean": round(statistics.mean(values), decimals),
            "stdev": round(statistics.stdev(values), decimals) if len(values) > 1 else 0,
            "p75": round(statistics.quantiles(values, n=4)[2], decimals) if len(values) >= 4 else round(max(values), decimals),
        }
    
    lcp_stats = calc_stats(lcp_values, 4)
    cls_stats = calc_stats(cls_values, 8)
    fid_stats = calc_stats(fid_values, 4)
    inp_stats = calc_stats(inp_values, 4)
    ttfb_stats = calc_stats(ttfb_values, 4)
    fcp_stats = calc_stats(fcp_values, 4)
    
    return {
        "LCP_median": lcp_stats["median"],
        "LCP_mean": lcp_stats["mean"],
        "LCP_stdev": lcp_stats["stdev"],
        "LCP_p75": lcp_stats["p75"],
        "LCP_rating": get_rating("lcp", lcp_stats["p75"]),
        
        "CLS_median": cls_stats["median"],
        "CLS_mean": cls_stats["mean"],
        "CLS_stdev": cls_stats["stdev"],
        "CLS_rating": get_rating("cls", cls_stats["median"]),
        
        "FID_median": fid_stats["median"],
        "FID_mean": fid_stats["mean"],
        "FID_stdev": fid_stats["stdev"],
        "FID_rating": get_rating("fid", fid_stats["p75"]) if fid_values else "N/A",
        
        "INP_median": inp_stats["median"],
        "INP_mean": inp_stats["mean"],
        "INP_stdev": inp_stats["stdev"],
        "INP_p75": inp_stats["p75"],
        "INP_rating": get_rating("inp", inp_stats["p75"]) if inp_values else "N/A",
        
        "TTFB_median": ttfb_stats["median"],
        "TTFB_mean": ttfb_stats["mean"],
        "TTFB_stdev": ttfb_stats["stdev"],
        "TTFB_rating": get_rating("ttfb", ttfb_stats["p75"]),
        
        "FCP_median": fcp_stats["median"],
        "FCP_mean": fcp_stats["mean"],
        
        "valid_runs": len(valid_runs),
        "total_runs": len(runs),
        "outliers_removed": {
            "LCP": len(lcp_raw) - len(lcp_values),
            "CLS": len(cls_raw) - len(cls_values),
            "INP": len(inp_raw) - len(inp_values),
        },
    }


async def run_cwv_tests(
    workspace_dir: str,
    application_results_path: str,
    visual_regression_results_path: str,
    url: str,
    device: str,
    num_runs: int = 5,
    headless: bool = True,
    run_visual_regression_tests: bool = True,
    framework: str = "Static HTML",
    results_dir: Optional[str] = None,
    warmup: bool = True,
) -> Dict[str, Any]:
    """Run Core Web Vitals performance tests on branches.

    Args:
        workspace_dir: Path to workspace directory
        application_results_path: Path to application results
        visual_regression_results_path: Path to regression results
        url: Target URL for testing
        device: Device type (mobile/desktop)
        num_runs: Number of test runs per branch
        headless: Run browser headlessly
        run_visual_regression_tests: Whether to run visual tests
        framework: Framework type for server commands
        results_dir: Directory for saving results (clean structure)
        warmup: Whether to run a warmup run before measurements

    Returns:
        Result dictionary with testing results directory
    """
    logger.info("Running CWV performance tests for: %s", url)

    try:
        workspace_path = Path(workspace_dir)
        dump_dir = workspace_path.parent
        
        # Use provided results_dir or fallback to old structure
        if results_dir:
            res_dir = Path(results_dir)
        else:
            res_dir = dump_dir / f"cwv_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        res_dir.mkdir(exist_ok=True)

        # Load regression results to get branches that passed
        branches_to_test = []
        if visual_regression_results_path and Path(visual_regression_results_path).exists():
            with open(visual_regression_results_path) as f:
                regression_data = json.load(f)
                # Only test branches without regressions
                for result in regression_data.get("results", []):
                    if not result.get("has_regression", False) and result.get("status") == "success":
                        branches_to_test.append(result["branch"])
                        
            logger.info("Testing %d branches that passed visual regression", len(branches_to_test))
        else:
            # Fallback: get all suggestion branches
            from cwv_tool.server_utils import get_suggestion_branches
            branches_to_test = get_suggestion_branches(workspace_dir)
            logger.info("Testing all %d suggestion branches", len(branches_to_test))

        if not branches_to_test:
            logger.warning("No branches to test")
            # Create empty summary for downstream nodes
            summary_path = res_dir / "cwv_summary.json"
            save_json_file({
                "timestamp": datetime.now().isoformat(),
                "url": url,
                "device": device,
                "num_runs": num_runs,
                "results": [],
                "message": "No branches to test",
            }, summary_path)
            
            return {
                "status": "success",
                "output_paths": {"testing_results_directory": str(res_dir)},
                "summary": {"message": "No branches to test"},
            }

        all_results: List[Dict[str, Any]] = []
        default_branch = get_default_branch(workspace_dir)

        # Test baseline (main/master branch)
        logger.info("Testing baseline (%s branch) with %d runs", default_branch, num_runs)
        checkout_branch(workspace_dir, default_branch)
        
        baseline_server = await start_framework_server(workspace_dir, framework)
        
        if baseline_server.get("status") != "success":
            return {
                "status": "error",
                "error": f"Failed to start baseline server: {baseline_server.get('error')}",
            }
        
        # Optional warmup run
        if warmup:
            logger.info("  Warmup run...")
            await measure_cwv_metrics(baseline_server["url"], device, headless)
            await asyncio.sleep(1)
        
        # Use measure_multiple_runs which keeps working settle_time across runs
        baseline_runs, baseline_settle_time, baseline_success = await measure_multiple_runs(
            baseline_server["url"], device, headless, num_runs
        )
        
        kill_server(baseline_server.get("pid"))
        await asyncio.sleep(1)
        
        if baseline_success:
            baseline_aggregated = calculate_aggregated_metrics(baseline_runs)
        else:
            baseline_aggregated = {
                "LCP_median": float("nan"), "LCP_mean": float("nan"), "LCP_stdev": float("nan"), "LCP_p75": float("nan"),
                "CLS_median": float("nan"), "CLS_mean": float("nan"), "CLS_stdev": float("nan"),
                "FID_median": float("nan"), "FID_mean": float("nan"), "FID_stdev": float("nan"),
                "INP_median": float("nan"), "INP_mean": float("nan"), "INP_stdev": float("nan"), "INP_p75": float("nan"),
                "TTFB_median": float("nan"), "TTFB_mean": float("nan"), "TTFB_stdev": float("nan"),
                "FCP_median": float("nan"), "FCP_mean": float("nan"),
                "valid_runs": 0, "total_runs": len(baseline_runs),
            }
        baseline_results = {
            "branch": default_branch,
            "is_baseline": True,
            "runs": baseline_runs,
            "metrics": baseline_aggregated,
            "final_settle_time": baseline_settle_time,
        }
        all_results.append(baseline_results)

        if not baseline_success:
            return {
                "status": "error",
                "error": "Baseline CWV measurement exceeded max retries",
                "output_paths": {
                    "testing_results_directory": str(res_dir),
                },
                "summary": {
                    "baseline_failed": True,
                },
            }

        # Test each optimization branch
        for branch in branches_to_test:
            logger.info("Testing branch: %s with %d runs", branch, num_runs)

            if not checkout_branch(workspace_dir, branch):
                all_results.append({
                    "branch": branch,
                    "status": "error",
                    "error": "Failed to checkout branch",
                })
                continue

            # Start server for this branch
            server_result = await start_framework_server(workspace_dir, framework)
            
            if server_result.get("status") != "success":
                all_results.append({
                    "branch": branch,
                    "status": "error",
                    "error": f"Failed to start server: {server_result.get('error')}",
                })
                continue
            
            # Warmup run for this branch too
            if warmup:
                await measure_cwv_metrics(server_result["url"], device, headless)
                await asyncio.sleep(0.5)
            
            # Use measure_multiple_runs which keeps working settle_time across runs
            branch_runs, branch_settle_time, branch_success = await measure_multiple_runs(
                server_result["url"], device, headless, num_runs
            )
            
            # Kill server for this branch
            kill_server(server_result.get("pid"))
            await asyncio.sleep(1)
            
            if branch_success:
                branch_aggregated = calculate_aggregated_metrics(branch_runs)
            else:
                branch_aggregated = {
                    "LCP_median": float("nan"), "LCP_mean": float("nan"), "LCP_stdev": float("nan"), "LCP_p75": float("nan"),
                    "CLS_median": float("nan"), "CLS_mean": float("nan"), "CLS_stdev": float("nan"),
                    "FID_median": float("nan"), "FID_mean": float("nan"), "FID_stdev": float("nan"),
                    "INP_median": float("nan"), "INP_mean": float("nan"), "INP_stdev": float("nan"), "INP_p75": float("nan"),
                    "TTFB_median": float("nan"), "TTFB_mean": float("nan"), "TTFB_stdev": float("nan"),
                    "FCP_median": float("nan"), "FCP_mean": float("nan"),
                    "valid_runs": 0, "total_runs": len(branch_runs),
                }
            
            # Calculate improvement vs baseline
            improvement = {}
            
            if baseline_aggregated.get("LCP_median", 0) > 0:
                improvement["LCP_improvement_pct"] = round(
                    (baseline_aggregated["LCP_median"] - branch_aggregated.get("LCP_median", 0)) 
                    / baseline_aggregated["LCP_median"] * 100, 2
                )
            if baseline_aggregated.get("CLS_median", 0) > 0:
                improvement["CLS_improvement_pct"] = round(
                    (baseline_aggregated["CLS_median"] - branch_aggregated.get("CLS_median", 0)) 
                    / baseline_aggregated["CLS_median"] * 100, 2
                )
            if baseline_aggregated.get("INP_median", 0) > 0 and branch_aggregated.get("INP_median", 0) > 0:
                improvement["INP_improvement_pct"] = round(
                    (baseline_aggregated["INP_median"] - branch_aggregated.get("INP_median", 0)) 
                    / baseline_aggregated["INP_median"] * 100, 2
                )
            if baseline_aggregated.get("TTFB_median", 0) > 0 and branch_aggregated.get("TTFB_median", 0) > 0:
                improvement["TTFB_improvement_pct"] = round(
                    (baseline_aggregated["TTFB_median"] - branch_aggregated.get("TTFB_median", 0)) 
                    / baseline_aggregated["TTFB_median"] * 100, 2
                )
            
            all_results.append({
                "branch": branch,
                "status": "success" if branch_success else "error",
                "runs": branch_runs,
                "metrics": branch_aggregated,
                "improvement": improvement if branch_success else {},
                "final_settle_time": branch_settle_time,
            })

        # Return to default branch
        checkout_branch(workspace_dir, default_branch)

        # Save all results
        summary_path = res_dir / "cwv_summary.json"
        save_json_file(
            {
                "timestamp": datetime.now().isoformat(),
                "url": url,
                "device": device,
                "num_runs": num_runs,
                "framework": framework,
                "warmup_enabled": warmup,
                "baseline": baseline_results,
                "results": all_results,
            },
            summary_path,
        )

        return {
            "status": "success",
            "output_paths": {
                "testing_results_directory": str(res_dir),
            },
            "summary": {
                "total_branches": len(branches_to_test),
                "tested": len([r for r in all_results if r.get("status") != "error"]) - 1,  # Exclude baseline
                "baseline_lcp": baseline_aggregated.get("LCP_median"),
                "baseline_lcp_rating": baseline_aggregated.get("LCP_rating"),
            },
        }

    except Exception as e:
        logger.error("CWV testing failed: %s", e, exc_info=True)
        return {"status": "error", "error": str(e)}
