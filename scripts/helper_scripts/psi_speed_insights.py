#!/usr/bin/env python3
"""
PSI Speed Insights CLI

Queries the Google PageSpeed Insights API (same endpoint as cwv-agent/src/tools/psi.js)
for Lighthouse-based performance data on any public URL.

API key is read from GOOGLE_PAGESPEED_INSIGHTS_API_KEY (same env var as cwv-agent).
Without a key the API still works but is rate-limited.

Usage:
    python scripts/helper_scripts/psi_speed_insights.py
    python scripts/helper_scripts/psi_speed_insights.py -u https://example.com
    python scripts/helper_scripts/psi_speed_insights.py --strategy desktop --categories performance accessibility
    python scripts/helper_scripts/psi_speed_insights.py --json results.json
"""

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import requests
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# Canonical PSI endpoint — same as cwv-agent/src/tools/psi.js
PSI_BASE_URL = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
PSI_API_KEY = os.getenv("GOOGLE_PAGESPEED_INSIGHTS_API_KEY", "")  # same env var as cwv-agent
DEFAULT_URL = "https://someshsingh22.github.io"
DEFAULT_STRATEGY = "mobile"
REQUEST_TIMEOUT = 120
USER_AGENT = "Spacecat/1.0"

console = Console()

METRIC_THRESHOLDS: dict[str, dict[str, float]] = {
    "largest-contentful-paint": {"good": 2500, "poor": 4000},
    "first-contentful-paint": {"good": 1800, "poor": 3000},
    "total-blocking-time": {"good": 200, "poor": 600},
    "cumulative-layout-shift": {"good": 0.1, "poor": 0.25},
    "speed-index": {"good": 3400, "poor": 5800},
    "interactive": {"good": 3800, "poor": 7300},
}

OPPORTUNITY_AUDITS = [
    "uses-optimized-images",
    "uses-modern-image-formats",
    "uses-text-compression",
    "render-blocking-resources",
    "unminified-css",
    "unminified-javascript",
    "unused-css-rules",
    "unused-javascript",
    "uses-responsive-images",
    "efficient-animated-content",
    "duplicated-javascript",
    "legacy-javascript",
    "server-response-time",
    "redirects",
    "uses-rel-preconnect",
    "prioritize-lcp-image",
    "unsized-images",
]


@dataclass
class PSIResult:
    """Parsed result from a PSI API response."""

    url: str
    strategy: str
    fetch_time: str
    lighthouse_version: str
    categories: dict[str, float]
    metrics: dict[str, dict[str, Any]]
    opportunities: list[dict[str, Any]]
    lcp_element: dict[str, Any] | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


def _cleanup_psi(data: dict[str, Any]) -> dict[str, Any]:
    """Remove base-64 screenshots — mirrors psi.js cleanup()."""
    lr = data.get("lighthouseResult", {})
    lr.get("audits", {}).pop("screenshot-thumbnails", None)
    lr.get("audits", {}).pop("final-screenshot", None)
    lr.pop("fullPageScreenshot", None)
    return data


def run_psi(
    url: str,
    strategy: str = DEFAULT_STRATEGY,
) -> dict[str, Any]:
    """Call the Google PSI API — mirrors cwv-agent/src/tools/psi.js exactly.

    No explicit categories are sent; the API returns all defaults
    (performance, best-practices, accessibility, seo, pwa).
    Screenshot blobs are cleaned up from the response.

    Args:
        url: The public URL to audit.
        strategy: Device strategy -- 'mobile' or 'desktop'.

    Returns:
        Cleaned raw JSON dict from the PSI API.

    Raises:
        requests.HTTPError: If the PSI service returns a non-2xx status.
        requests.Timeout: If the request exceeds REQUEST_TIMEOUT seconds.
    """
    params: list[tuple[str, str]] = [
        ("url", url),
        ("strategy", strategy),
    ]
    if PSI_API_KEY:
        params.append(("key", PSI_API_KEY))

    resp = requests.get(
        PSI_BASE_URL,
        params=params,
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return _cleanup_psi(resp.json())


def _rating(value: float, good: float, poor: float) -> str:
    """Map a numeric metric value to a qualitative rating label."""
    if value <= good:
        return "Good"
    if value <= poor:
        return "Needs Improvement"
    return "Poor"


def _rating_color(rating: str) -> str:
    """Return a rich-compatible color for a rating string."""
    return {"Good": "green", "Needs Improvement": "yellow", "Poor": "red"}.get(rating, "white")


def _score_color(score: float) -> str:
    """Return a rich-compatible color for a 0-100 score."""
    if score >= 90:
        return "green"
    if score >= 50:
        return "yellow"
    return "red"


def parse_result(raw: dict[str, Any], strategy: str) -> PSIResult:
    """Parse the raw PSI JSON into a structured PSIResult.

    Args:
        raw: Raw JSON dict returned by run_psi().
        strategy: The strategy used for the request.

    Returns:
        A populated PSIResult dataclass.
    """
    lr = raw.get("lighthouseResult", {})
    audits = lr.get("audits", {})
    cats = lr.get("categories", {})

    category_scores = {}
    for cat_id, cat_data in cats.items():
        score = cat_data.get("score")
        if score is not None:
            category_scores[cat_data.get("title", cat_id)] = round(score * 100, 1)

    metrics: dict[str, dict[str, Any]] = {}
    for audit_id, thresholds in METRIC_THRESHOLDS.items():
        audit = audits.get(audit_id)
        if not audit or audit.get("scoreDisplayMode") in ("notApplicable", "manual"):
            continue
        numeric = audit.get("numericValue")
        if numeric is None:
            continue
        rating = _rating(numeric, thresholds["good"], thresholds["poor"])
        metrics[audit_id] = {
            "title": audit.get("title", audit_id),
            "value": numeric,
            "display": audit.get("displayValue", ""),
            "rating": rating,
        }

    opportunities: list[dict[str, Any]] = []
    for audit_id in OPPORTUNITY_AUDITS:
        audit = audits.get(audit_id)
        if not audit:
            continue
        if audit.get("scoreDisplayMode") in ("notApplicable", "informational", "manual"):
            continue
        if audit.get("score") is not None and audit["score"] < 1:
            entry: dict[str, Any] = {
                "id": audit_id,
                "title": audit.get("title", audit_id),
                "display": audit.get("displayValue", ""),
            }
            details = audit.get("details", {})
            if details.get("overallSavingsMs"):
                entry["savings_ms"] = details["overallSavingsMs"]
            if details.get("overallSavingsBytes"):
                entry["savings_kb"] = round(details["overallSavingsBytes"] / 1024, 1)
            opportunities.append(entry)

    lcp_elem = None
    lcp_audit = audits.get("largest-contentful-paint-element")
    if lcp_audit:
        items = (lcp_audit.get("details") or {}).get("items", [])
        if items:
            sub_items = items[0].get("items", [{}])
            node = sub_items[0].get("node") if sub_items else None
            if node:
                lcp_elem = {
                    "snippet": node.get("snippet", ""),
                    "selector": node.get("selector", ""),
                }
                rect = node.get("boundingRect")
                if rect:
                    lcp_elem["size"] = f"{rect.get('width', 0)}x{rect.get('height', 0)}"

    url = raw.get("id") or lr.get("requestedUrl") or lr.get("finalDisplayedUrl") or ""

    return PSIResult(
        url=url,
        strategy=strategy,
        fetch_time=lr.get("fetchTime", ""),
        lighthouse_version=lr.get("lighthouseVersion", ""),
        categories=category_scores,
        metrics=metrics,
        opportunities=opportunities,
        lcp_element=lcp_elem,
        raw=raw,
    )


def print_report(result: PSIResult) -> None:
    """Pretty-print the PSI result to the console using rich.

    Args:
        result: A parsed PSIResult to display.
    """
    console.print()
    console.rule("[bold]PSI Speed Insights Report[/bold]")
    console.print(f"  URL       : [cyan]{result.url}[/cyan]")
    console.print(f"  Strategy  : [cyan]{result.strategy}[/cyan]")
    console.print(f"  Fetched   : {result.fetch_time}")
    console.print(f"  Lighthouse: v{result.lighthouse_version}")
    console.print()

    # -- Category scores --
    score_table = Table(title="Category Scores", show_lines=True)
    score_table.add_column("Category", style="bold")
    score_table.add_column("Score", justify="center")
    for cat_name, score in result.categories.items():
        color = _score_color(score)
        score_table.add_row(cat_name, f"[{color}]{score}[/{color}]")
    console.print(score_table)
    console.print()

    # -- Core metrics --
    metric_table = Table(title="Core Metrics", show_lines=True)
    metric_table.add_column("Metric", style="bold")
    metric_table.add_column("Value", justify="right")
    metric_table.add_column("Rating", justify="center")
    for m in result.metrics.values():
        color = _rating_color(m["rating"])
        metric_table.add_row(m["title"], m["display"], f"[{color}]{m['rating']}[/{color}]")
    console.print(metric_table)
    console.print()

    # -- LCP element --
    if result.lcp_element:
        lcp_text = Text()
        lcp_text.append(f"  Snippet : {result.lcp_element.get('snippet', 'N/A')}\n")
        lcp_text.append(f"  Selector: {result.lcp_element.get('selector', 'N/A')}\n")
        if "size" in result.lcp_element:
            lcp_text.append(f"  Size    : {result.lcp_element['size']}px")
        console.print(Panel(lcp_text, title="LCP Element"))
        console.print()

    # -- Opportunities --
    if result.opportunities:
        opp_table = Table(title="Optimization Opportunities", show_lines=True)
        opp_table.add_column("Issue", style="bold", max_width=50)
        opp_table.add_column("Detail", max_width=25)
        opp_table.add_column("Savings", justify="right")
        for opp in result.opportunities:
            savings_parts = []
            if "savings_ms" in opp:
                savings_parts.append(f"{opp['savings_ms']:.0f} ms")
            if "savings_kb" in opp:
                savings_parts.append(f"{opp['savings_kb']:.1f} KiB")
            opp_table.add_row(
                opp["title"],
                opp.get("display", ""),
                ", ".join(savings_parts) or "-",
            )
        console.print(opp_table)
    else:
        console.print("[green]No significant optimization opportunities found.[/green]")

    console.print()
    console.rule()


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments.

    Returns:
        Parsed namespace with url, strategy, categories, json, verbose flags.
    """
    parser = argparse.ArgumentParser(
        description="Query the internal PSI service for Lighthouse speed insights.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/helper_scripts/psi_speed_insights.py
  python scripts/helper_scripts/psi_speed_insights.py -u https://example.com
  python scripts/helper_scripts/psi_speed_insights.py --strategy desktop
  python scripts/helper_scripts/psi_speed_insights.py --categories performance accessibility
  python scripts/helper_scripts/psi_speed_insights.py --json report.json
  python scripts/helper_scripts/psi_speed_insights.py --both
        """,
    )
    parser.add_argument(
        "-u", "--url",
        default=DEFAULT_URL,
        help=f"URL to audit (default: {DEFAULT_URL})",
    )
    parser.add_argument(
        "--strategy",
        choices=["mobile", "desktop"],
        default=DEFAULT_STRATEGY,
        help=f"Device strategy (default: {DEFAULT_STRATEGY})",
    )
    parser.add_argument(
        "--both",
        action="store_true",
        help="Run both mobile and desktop strategies",
    )
    parser.add_argument(
        "--categories",
        nargs="+",
        default=DEFAULT_CATEGORIES,
        help="Lighthouse categories to request (default: all four)",
    )
    parser.add_argument(
        "--json",
        type=str,
        metavar="FILE",
        help="Save raw JSON response to a file",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Print raw JSON to stdout instead of formatted report",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Show request details and timing",
    )
    return parser.parse_args()


def _run_single(
    url: str,
    strategy: str,
    categories: list[str],
    verbose: bool,
) -> PSIResult:
    """Execute a single PSI request, parse, and print the report.

    Args:
        url: Target URL.
        strategy: 'mobile' or 'desktop'.
        categories: List of Lighthouse categories.
        verbose: Whether to print timing info.

    Returns:
        The parsed PSIResult.
    """
    console.print(f"[bold]Fetching PSI data for [cyan]{url}[/cyan] ({strategy})...[/bold]")

    start = time.monotonic()
    raw = run_psi(url, strategy=strategy, categories=categories)
    elapsed = time.monotonic() - start

    if verbose:
        console.print(f"  Request completed in {elapsed:.2f}s")

    result = parse_result(raw, strategy)
    print_report(result)
    return result


def main() -> int:
    """CLI entry point.

    Returns:
        Exit code: 0 on success, 1 on failure.
    """
    args = parse_args()

    strategies = ["mobile", "desktop"] if args.both else [args.strategy]
    all_raw: dict[str, Any] = {}

    for strategy in strategies:
        result = _run_single(args.url, strategy, args.categories, args.verbose)
        all_raw[strategy] = result.raw

    if args.raw:
        output = all_raw if args.both else all_raw[strategies[0]]
        print(json.dumps(output, indent=2))

    if args.json:
        payload = all_raw if args.both else all_raw[strategies[0]]
        with open(args.json, "w") as f:
            json.dump(payload, f, indent=2)
        console.print(f"[green]Raw JSON saved to {args.json}[/green]")

    return 0


if __name__ == "__main__":
    sys.exit(main())
