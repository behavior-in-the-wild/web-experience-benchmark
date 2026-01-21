#!/usr/bin/env python3
"""
Core Web Vitals Benchmark Tool

Measures LCP, CLS, FID, INP, and TTFB with configurable throttling,
retry logic, and statistical analysis.
"""

from playwright.sync_api import sync_playwright
import statistics
import time
import json
import argparse
import sys
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Any

# ---------- configuration defaults ----------

DEFAULT_URL = "http://localhost:8000"
DEFAULT_RUNS = 7
DEFAULT_TIMEOUT = 60000  # 60s timeout (increased from 30s)
DEFAULT_WAIT_STRATEGY = "domcontentloaded"  # More reliable than networkidle
DEFAULT_SETTLE_TIME = 3000  # ms to wait for page to stabilize

# Chromium launch options
HEADLESS = True
SANDBOX = True

# Network throttling presets (download/upload in Kbps, latency in ms)
THROTTLE_PRESETS = {
    'none': None,
    'fast-3g': {'download': 1600, 'upload': 750, 'latency': 150},
    'slow-3g': {'download': 500, 'upload': 500, 'latency': 400},
    '4g': {'download': 4000, 'upload': 3000, 'latency': 100},
    'cable': {'download': 5000, 'upload': 1000, 'latency': 28},
}

# Rating thresholds (based on Google's CWV thresholds)
THRESHOLDS = {
    'lcp': {'good': 2.5, 'needs_improvement': 4.0},  # seconds
    'cls': {'good': 0.1, 'needs_improvement': 0.25},
    'fid': {'good': 100, 'needs_improvement': 300},  # ms
    'inp': {'good': 200, 'needs_improvement': 500},  # ms
    'ttfb': {'good': 800, 'needs_improvement': 1800},  # ms
}


@dataclass
class RunConfig:
    """Configuration for a benchmark run."""
    url: str = DEFAULT_URL
    n_runs: int = DEFAULT_RUNS
    network_throttle: str = 'none'
    cpu_throttle: int = 1
    headless: bool = HEADLESS
    sandbox: bool = SANDBOX
    timeout: int = DEFAULT_TIMEOUT
    wait_strategy: str = DEFAULT_WAIT_STRATEGY
    settle_time: int = DEFAULT_SETTLE_TIME
    max_retries: int = 3
    warmup: bool = True
    simulate_interaction: bool = True
    verbose: bool = False


@dataclass
class VitalsResult:
    """Result from a single CWV measurement."""
    lcp: Optional[float] = None  # seconds
    cls: Optional[float] = None
    fid: Optional[float] = None  # ms
    inp: Optional[float] = None  # ms
    ttfb: Optional[float] = None  # ms
    error: Optional[str] = None
    
    @property
    def is_valid(self) -> bool:
        return self.error is None and self.lcp is not None


# ---------- helpers ----------

def fmt(val: Optional[float], suffix: str = "", prec: int = 4) -> str:
    """Format a value with suffix and precision."""
    if val is None:
        return "NA"
    return f"{val:.{prec}f}{suffix}"


def remove_outliers(values: List[float]) -> List[float]:
    """Remove outliers using IQR method."""
    if len(values) < 4:
        return values

    q1, q3 = statistics.quantiles(values, n=4)[0], statistics.quantiles(values, n=4)[2]
    iqr = q3 - q1
    low, high = q1 - 1.5 * iqr, q3 + 1.5 * iqr

    return [v for v in values if low <= v <= high]


def summarize(values: List[float]) -> Optional[Dict[str, float]]:
    """Calculate summary statistics for a list of values."""
    if not values:
        return None
    return {
        "mean": statistics.mean(values),
        "median": statistics.median(values),
        "stdev": statistics.stdev(values) if len(values) > 1 else 0,
        "min": min(values),
        "max": max(values),
        "p75": statistics.quantiles(values, n=4)[2] if len(values) >= 4 else max(values),
    }


def get_rating(metric: str, value: float) -> str:
    """Get Good/Needs Improvement/Poor rating based on thresholds."""
    thresholds = THRESHOLDS.get(metric)
    if not thresholds:
        return "Unknown"
    
    if value <= thresholds['good']:
        return "Good ✅"
    elif value <= thresholds['needs_improvement']:
        return "Needs Improvement ⚠️"
    else:
        return "Poor ❌"


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
            lcpElement: null
        };
        
        window.__webVitalsReady = new Promise((resolve) => {
            window.__resolveVitals = resolve;
        });

        // LCP - using web-vitals.js standard approach
        try {
            new PerformanceObserver((list) => {
                const entries = list.getEntries();
                const lastEntry = entries[entries.length - 1];
                window.__webVitals.lcp = lastEntry.renderTime || lastEntry.loadTime;
                window.__webVitals.lcpElement = lastEntry.element?.tagName || 'unknown';
            }).observe({ type: 'largest-contentful-paint', buffered: true });
        } catch (e) {
            console.log('LCP observer not supported');
        }

        // CLS with session window approach
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

        // INP - improved tracking
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

        // TTFB and FCP
        try {
            new PerformanceObserver((list) => {
                const [navEntry] = list.getEntries();
                window.__webVitals.ttfb = navEntry.responseStart;
            }).observe({ type: 'navigation', buffered: true });
        } catch (e) {
            console.log('Navigation observer not supported');
        }
        
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


# ---------- single run ----------

def measure_once(config: RunConfig, is_warmup: bool = False) -> VitalsResult:
    """Perform a single CWV measurement."""
    try:
        with sync_playwright() as p:
            # Launch args for sandboxing control
            launch_args = [
                '--disable-background-timer-throttling',
                '--disable-backgrounding-occluded-windows',
                '--disable-renderer-backgrounding',
            ]
            if not config.sandbox:
                launch_args.extend([
                    '--no-sandbox',
                    '--disable-setuid-sandbox',
                    '--disable-dev-shm-usage',
                ])
            
            browser = p.chromium.launch(
                headless=config.headless,
                args=launch_args
            )
            
            context = browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                device_scale_factor=1,
                user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            )
            page = context.new_page()

            # Apply network throttling if configured
            throttle_config = THROTTLE_PRESETS.get(config.network_throttle)
            cdp = None
            
            if throttle_config or config.cpu_throttle > 1:
                cdp = context.new_cdp_session(page)
                
                if throttle_config:
                    cdp.send('Network.enable')
                    cdp.send('Network.emulateNetworkConditions', {
                        'offline': False,
                        'downloadThroughput': throttle_config['download'] * 1024 / 8,
                        'uploadThroughput': throttle_config['upload'] * 1024 / 8,
                        'latency': throttle_config['latency']
                    })
                
                if config.cpu_throttle > 1:
                    cdp.send('Emulation.setCPUThrottlingRate', {'rate': config.cpu_throttle})

            # Inject CWV observers BEFORE page load
            page.add_init_script(get_webvitals_script())

            # Navigate with configurable wait strategy and timeout
            try:
                page.goto(config.url, wait_until=config.wait_strategy, timeout=config.timeout)
            except Exception as nav_error:
                # If networkidle times out, try with domcontentloaded
                if "Timeout" in str(nav_error) and config.wait_strategy == "networkidle":
                    if config.verbose:
                        print(f"    Retrying with domcontentloaded...")
                    page.goto(config.url, wait_until="domcontentloaded", timeout=config.timeout)
                else:
                    raise
            
            # Wait for page to stabilize
            page.wait_for_timeout(config.settle_time)

            # Simulate real user interaction for FID/INP if enabled
            if config.simulate_interaction and not is_warmup:
                try:
                    # Inject a test button with guaranteed blocking work for consistent INP
                    page.evaluate("""
                        (() => {
                            // Create test button that blocks for 50ms on click
                            const btn = document.createElement('button');
                            btn.id = '__cwv_test_btn';
                            btn.textContent = '';
                            Object.assign(btn.style, {
                                position: 'fixed',
                                top: '0',
                                left: '0',
                                width: '1px',
                                height: '1px',
                                opacity: '0.01',
                                zIndex: '2147483647',
                                pointerEvents: 'auto',
                                border: 'none',
                                padding: '0',
                                margin: '0'
                            });
                            
                            // Add blocking event handler (triggers INP measurement)
                            btn.addEventListener('pointerdown', () => {
                                const start = performance.now();
                                while (performance.now() - start < 50) {
                                    // Blocking work - ensures INP >= 50ms
                                }
                            });
                            
                            btn.addEventListener('click', () => {
                                const start = performance.now();
                                while (performance.now() - start < 30) {
                                    // Additional blocking work
                                }
                            });
                            
                            document.body.appendChild(btn);
                        })();
                    """)
                    
                    # Wait for button to be ready
                    page.wait_for_timeout(100)
                    
                    # Click the test button using real mouse - this triggers INP
                    test_btn = page.locator('#__cwv_test_btn')
                    test_btn.click(force=True, timeout=1000)
                    page.wait_for_timeout(200)
                    
                    # Also try clicking a real page element for FID
                    for selector in ['button:visible', 'a:visible', 'input:visible']:
                        try:
                            element = page.locator(selector).first
                            if element.is_visible(timeout=50):
                                element.click(timeout=300, force=True)
                                page.wait_for_timeout(100)
                                break
                        except:
                            continue
                    
                    # Scroll interaction for CLS observation
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight / 4)")
                    page.wait_for_timeout(200)
                    page.evaluate("window.scrollTo(0, 0)")
                    page.wait_for_timeout(200)
                    
                except Exception as interact_error:
                    if config.verbose:
                        print(f"    Interaction warning: {interact_error}")

            # Final wait to collect all metrics
            page.wait_for_timeout(1000)

            # Collect vitals
            vitals = page.evaluate("window.__webVitals")
            browser.close()
            
            # Parse results
            return VitalsResult(
                lcp=vitals["lcp"] / 1000 if vitals.get("lcp") else None,
                cls=vitals.get("cls", 0),
                fid=vitals.get("fid"),
                inp=vitals.get("inp"),
                ttfb=vitals.get("ttfb"),
            )

    except Exception as e:
        return VitalsResult(error=str(e))


def run_with_retry(config: RunConfig, run_number: int) -> VitalsResult:
    """Run measurement with retry logic."""
    last_error = None
    
    for attempt in range(config.max_retries):
        result = measure_once(config)
        
        if result.is_valid:
            return result
        
        last_error = result.error
        if attempt < config.max_retries - 1:
            if config.verbose:
                print(f"    Retry {attempt + 1}/{config.max_retries - 1}...")
            time.sleep(2)  # Wait before retry
    
    return VitalsResult(error=f"Failed after {config.max_retries} attempts: {last_error}")


# ---------- main ----------

def run_benchmark(config: RunConfig) -> Dict[str, Any]:
    """Run the complete benchmark and return results."""
    results = {
        'lcp': [], 'cls': [], 'fid': [], 'inp': [], 'ttfb': []
    }
    raw_results: List[VitalsResult] = []
    errors: List[str] = []

    # Print header
    print(f"\n{'='*60}")
    print(f"Core Web Vitals Benchmark")
    print(f"{'='*60}")
    print(f"URL: {config.url}")
    print(f"Runs: {config.n_runs}")
    print(f"Network: {config.network_throttle.upper()}")
    if THROTTLE_PRESETS.get(config.network_throttle):
        t = THROTTLE_PRESETS[config.network_throttle]
        print(f"  ↓ {t['download']} Kbps / ↑ {t['upload']} Kbps / {t['latency']}ms latency")
    print(f"CPU Throttle: {config.cpu_throttle}x" if config.cpu_throttle > 1 else "CPU Throttle: None")
    print(f"Timeout: {config.timeout}ms")
    print(f"Wait Strategy: {config.wait_strategy}")
    print(f"Sandbox: {'Enabled' if config.sandbox else 'Disabled'}")
    print(f"Headless: {'Yes' if config.headless else 'No'}")
    print(f"Retries: {config.max_retries}")
    print(f"{'='*60}\n")

    # Warmup run (optional, not counted)
    if config.warmup:
        print("Warmup run... ", end="", flush=True)
        warmup_result = measure_once(config, is_warmup=True)
        if warmup_result.is_valid:
            print("✓")
        else:
            print(f"⚠️ ({warmup_result.error[:50]}...)" if len(str(warmup_result.error)) > 50 else f"⚠️ ({warmup_result.error})")
        print()

    # Main runs
    for i in range(config.n_runs):
        result = run_with_retry(config, i + 1)
        raw_results.append(result)
        
        if result.error:
            print(f"Run {i+1}: ERROR - {result.error[:80]}..." if len(result.error) > 80 else f"Run {i+1}: ERROR - {result.error}")
            errors.append(result.error)
            continue

        # Store values
        if result.lcp is not None:
            results['lcp'].append(result.lcp)
        if result.cls is not None:
            results['cls'].append(result.cls)
        if result.fid is not None:
            results['fid'].append(result.fid)
        if result.inp is not None:
            results['inp'].append(result.inp)
        if result.ttfb is not None:
            results['ttfb'].append(result.ttfb)

        print(
            f"Run {i+1}: "
            f"LCP={fmt(result.lcp, 's', 4)}, "
            f"CLS={fmt(result.cls, '', 6)}, "
            f"FID={fmt(result.fid, 'ms', 4)}, "
            f"INP={fmt(result.inp, 'ms', 4)}, "
            f"TTFB={fmt(result.ttfb, 'ms', 4)}"
        )

        # Small delay between runs to avoid resource contention
        if i < config.n_runs - 1:
            time.sleep(0.5)

    # Post processing and summary
    print(f"\n{'='*60}")
    print("SUMMARY (after outlier removal)")
    print(f"{'='*60}")

    summary_results = {}
    
    for metric in ['lcp', 'cls', 'fid', 'inp', 'ttfb']:
        clean_vals = remove_outliers(results[metric])
        summary = summarize(clean_vals)
        summary_results[metric] = summary
        
        if summary:
            unit = 's' if metric == 'lcp' else 'ms' if metric in ['fid', 'inp', 'ttfb'] else ''
            print(f"\n{metric.upper()} ({unit or 'score'}):")
            
            for key in ['mean', 'median', 'stdev', 'p75', 'min', 'max']:
                val = summary.get(key, 0)
                decimals = 6 if metric == 'cls' else 4
                print(f"  {key:>6}: {val:.{decimals}f}")
            
            # Use p75 for rating (Google's standard)
            rating_value = summary['p75'] if metric != 'cls' else summary['median']
            rating = get_rating(metric, rating_value)
            print(f"  Rating: {rating}")
        else:
            print(f"\n{metric.upper()}: No data collected")

    # Error summary
    if errors:
        print(f"\n⚠️  {len(errors)} run(s) failed")
    
    success_rate = (config.n_runs - len(errors)) / config.n_runs * 100
    print(f"\n✓ Success rate: {success_rate:.0f}% ({config.n_runs - len(errors)}/{config.n_runs})")
    print(f"{'='*60}\n")

    return {
        'config': {
            'url': config.url,
            'n_runs': config.n_runs,
            'network_throttle': config.network_throttle,
            'cpu_throttle': config.cpu_throttle,
        },
        'raw_results': [
            {
                'lcp': r.lcp,
                'cls': r.cls,
                'fid': r.fid,
                'inp': r.inp,
                'ttfb': r.ttfb,
                'error': r.error
            } for r in raw_results
        ],
        'summary': summary_results,
        'errors': errors,
        'success_rate': success_rate,
    }


def parse_args() -> RunConfig:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description='Core Web Vitals Benchmark Tool',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python web_vitals.py                          # Test localhost:8000
  python web_vitals.py -u https://example.com   # Test a URL
  python web_vitals.py -n 10 --network fast-3g  # 10 runs with 3G throttling
  python web_vitals.py --no-warmup --verbose    # Skip warmup, show details
        """
    )
    
    parser.add_argument('-u', '--url', default=DEFAULT_URL,
                        help=f'URL to test (default: {DEFAULT_URL})')
    parser.add_argument('-n', '--runs', type=int, default=DEFAULT_RUNS,
                        help=f'Number of test runs (default: {DEFAULT_RUNS})')
    parser.add_argument('--network', choices=list(THROTTLE_PRESETS.keys()),
                        default='none', help='Network throttling preset')
    parser.add_argument('--cpu', type=int, default=1,
                        help='CPU throttle multiplier (1 = none, 4 = 4x slower)')
    parser.add_argument('--timeout', type=int, default=DEFAULT_TIMEOUT,
                        help=f'Navigation timeout in ms (default: {DEFAULT_TIMEOUT})')
    parser.add_argument('--wait', choices=['load', 'domcontentloaded', 'networkidle', 'commit'],
                        default=DEFAULT_WAIT_STRATEGY,
                        help=f'Wait strategy (default: {DEFAULT_WAIT_STRATEGY})')
    parser.add_argument('--settle', type=int, default=DEFAULT_SETTLE_TIME,
                        help=f'Page settle time in ms (default: {DEFAULT_SETTLE_TIME})')
    parser.add_argument('--retries', type=int, default=3,
                        help='Max retries per run (default: 3)')
    parser.add_argument('--no-warmup', action='store_true',
                        help='Skip warmup run')
    parser.add_argument('--no-interaction', action='store_true',
                        help='Skip user interaction simulation')
    parser.add_argument('--no-headless', action='store_true',
                        help='Run browser in visible mode')
    parser.add_argument('--no-sandbox', action='store_true',
                        help='Disable browser sandbox (for Docker/CI)')
    parser.add_argument('--verbose', '-v', action='store_true',
                        help='Show detailed output')
    parser.add_argument('--json', type=str, metavar='FILE',
                        help='Save results to JSON file')
    
    args = parser.parse_args()
    
    return RunConfig(
        url=args.url,
        n_runs=args.runs,
        network_throttle=args.network,
        cpu_throttle=args.cpu,
        headless=not args.no_headless,
        sandbox=not args.no_sandbox,
        timeout=args.timeout,
        wait_strategy=args.wait,
        settle_time=args.settle,
        max_retries=args.retries,
        warmup=not args.no_warmup,
        simulate_interaction=not args.no_interaction,
        verbose=args.verbose,
    ), args.json


if __name__ == "__main__":
    config, json_output = parse_args()
    results = run_benchmark(config)
    
    if json_output:
        with open(json_output, 'w') as f:
            json.dump(results, f, indent=2)
        print(f"Results saved to {json_output}")