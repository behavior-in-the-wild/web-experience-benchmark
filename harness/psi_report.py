#!/usr/bin/env python3
"""
psi_report.py — Thin Google PageSpeed Insights caller for the evaluate.sh harness.

Modeled on RateLimitTracker.call_psi() from patch_bore_parallel.py:
  - Up to 5 retries with exponential backoff + full jitter on 429 / 5xx
  - Respects Retry-After response header
  - Logs every request/response with elapsed time
  - Always exits 0; writes {"error": "..."} to --output on failure so the
    harness can continue without aborting the job

Usage:
    python3 psi_report.py \\
        --url  http://bore.pub:12345/ \\
        --strategy mobile \\
        --output results/1_agent_init_psi_mobile.json \\
        [--api-key YOUR_KEY]

Environment:
    GOOGLE_PAGESPEED_INSIGHTS_API_KEY  — used when --api-key is not given
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

PSI_URL = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
PSI_CATEGORIES = ["performance", "best-practices", "accessibility", "seo"]

# Minimum gap between successive calls to stay within Google's rate limits
GOOGLE_MIN_DELAY_S = 8.0
MAX_RETRIES = 5
REQUEST_TIMEOUT_S = 120


def _parse_retry_after(header_value: str) -> Optional[float]:
    """Parse a Retry-After header value (seconds integer or HTTP-date) into seconds."""
    val = header_value.strip()
    if not val:
        return None
    try:
        return float(val)
    except ValueError:
        pass
    # HTTP-date format — approximate by returning 60 s
    return 60.0


def _log(msg: str) -> None:
    print(msg, flush=True)


def call_psi(
    url: str,
    strategy: str,
    api_key: Optional[str],
    max_retries: int = MAX_RETRIES,
    timeout: int = REQUEST_TIMEOUT_S,
    min_delay: float = GOOGLE_MIN_DELAY_S,
) -> Tuple[Optional[Dict[str, Any]], str]:
    """
    Call the Google PSI API and return (payload, error_tag).

    error_tag is "" on success, a short string on failure.
    Implements the same retry/backoff logic as RateLimitTracker.call_psi()
    in patch_bore_parallel.py.
    """
    if not api_key:
        _log("[PSI] ERROR: No API key — set GOOGLE_PAGESPEED_INSIGHTS_API_KEY")
        return None, "no_api_key"

    last_call_mono: float = 0.0
    last_tag = "unexpected"

    for attempt in range(1, max_retries + 1):
        # Global pacing: ensure at least min_delay seconds between calls
        elapsed_since_last = time.monotonic() - last_call_mono
        if elapsed_since_last < min_delay:
            time.sleep(min_delay - elapsed_since_last)

        params: list[tuple[str, str]] = [("url", url), ("strategy", strategy)]
        for cat in PSI_CATEGORIES:
            params.append(("category", cat))
        params.append(("key", api_key))

        query = urllib.parse.urlencode(params)
        full_url = f"{PSI_URL}?{query}"

        _log(f"[PSI] → Attempt {attempt}/{max_retries}  strategy={strategy}  url={url}")
        t0 = time.monotonic()

        try:
            req = urllib.request.Request(full_url, headers={"User-Agent": "psi-report/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                elapsed = time.monotonic() - t0
                last_call_mono = time.monotonic()
                status = resp.status
                _log(f"[PSI] ← status={status}  elapsed={elapsed:.2f}s  attempt={attempt}")
                raw = resp.read()
                try:
                    payload = json.loads(raw)
                except Exception as parse_exc:
                    _log(f"[PSI] ERROR: JSON parse failure: {parse_exc}")
                    last_tag = "parse_failure"
                    time.sleep(min(5.0, min_delay))
                    continue
                return payload, ""

        except urllib.error.HTTPError as exc:
            elapsed = time.monotonic() - t0
            last_call_mono = time.monotonic()
            status = exc.code
            _log(f"[PSI] ← status={status}  elapsed={elapsed:.2f}s  attempt={attempt}")

            if status == 429:
                retry_after_hdr = exc.headers.get("Retry-After", "") if exc.headers else ""
                retry_after = _parse_retry_after(retry_after_hdr)
                if retry_after is None:
                    cap = min(120.0, 15.0 * (2 ** (attempt - 1)))
                    retry_after = random.uniform(0, cap)
                cooldown = retry_after + random.uniform(0.5, 2.0)
                _log(
                    f"[PSI] [BACKOFF] 429 rate-limited — "
                    f"Retry-After={retry_after:.1f}s, sleeping {cooldown:.1f}s"
                )
                last_tag = "http_429"
                time.sleep(cooldown)
                continue

            if 500 <= status < 600:
                cap = min(60.0, 10.0 * (2 ** (attempt - 1)))
                cooldown = random.uniform(cap * 0.5, cap) + random.uniform(0.5, 2.0)
                _log(f"[PSI] [BACKOFF] HTTP {status} — sleeping {cooldown:.1f}s")
                last_tag = "http_5xx"
                time.sleep(cooldown)
                continue

            # Other HTTP error (4xx) — not retriable
            _log(f"[PSI] ERROR: HTTP {status} (not retriable)")
            return None, f"http_{status}"

        except TimeoutError:
            elapsed = time.monotonic() - t0
            _log(f"[PSI] ERROR: request timed out after {elapsed:.2f}s  attempt={attempt}")
            last_tag = "psi_timeout"
            time.sleep(min_delay)
            continue

        except Exception as exc:
            elapsed = time.monotonic() - t0
            _log(f"[PSI] ERROR: unexpected {type(exc).__name__}: {exc}  elapsed={elapsed:.2f}s")
            last_tag = "unexpected"
            time.sleep(min_delay)
            continue

    _log(f"[PSI] ERROR: all {max_retries} attempts exhausted (last_tag={last_tag})")
    return None, last_tag


def main() -> None:
    p = argparse.ArgumentParser(description="Call Google PSI and write result JSON.")
    p.add_argument("--url",      required=True, help="Public URL to audit")
    p.add_argument("--strategy", required=True, choices=["mobile", "desktop"])
    p.add_argument("--output",   required=True, help="Path to write result JSON")
    p.add_argument(
        "--api-key",
        default=None,
        help="Google PSI API key (default: GOOGLE_PAGESPEED_INSIGHTS_API_KEY env var)",
    )
    args = p.parse_args()

    api_key = args.api_key or os.getenv("GOOGLE_PAGESPEED_INSIGHTS_API_KEY", "")

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    _log(f"[PSI] url={args.url}  strategy={args.strategy}  output={args.output}")

    payload, err_tag = call_psi(
        url=args.url,
        strategy=args.strategy,
        api_key=api_key or None,
    )

    if payload is not None:
        out_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        score = (
            payload.get("lighthouseResult", {})
            .get("categories", {})
            .get("performance", {})
            .get("score")
        )
        _log(f"[PSI] ✓ OK  performance_score={score}  output={args.output}")
    else:
        out_path.write_text(
            json.dumps({"error": err_tag, "url": args.url, "strategy": args.strategy},
                       indent=2),
            encoding="utf-8",
        )
        _log(f"[PSI] ✗ Failed (error_tag={err_tag})  output={args.output}")

    # Always exit 0 so evaluate.sh continues
    sys.exit(0)


if __name__ == "__main__":
    main()
