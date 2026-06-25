#!/usr/bin/env python3
"""
TEST 8 — prototype + prove the deterministic CLS fix (scroll-to-trigger).

Hypothesis: recorded CLS regressions from patch-introduced lazy/deferred loading
are real but measured non-deterministically because the current measurement never
scrolls, so below-fold lazy content triggers incidentally. If we scroll the full
page (forcing all lazy/deferred resources to load) before finalizing CLS, the
shift should appear DETERMINISTICALLY.

We measure each site with the tool's own CLS observer (get_webvitals_script),
two protocols, 3 runs each:
  - no_scroll : wait settle, read CLS            (current behaviour)
  - scroll    : scroll full page, wait, read CLS (proposed fix)

Expect: 136 (lazy-loaded ship icons) -> scroll recovers ~0.99 reproducibly,
no_scroll stays ~0. External-driven sites (1294) won't be fixed by scroll alone
(they need hermetic freeze).

Usage: PYTHONPATH=src python test8_scroll_fix.py
"""
from __future__ import annotations
import asyncio, json, os, sys, shutil
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
import _val_lib as V
from cwv_tool.performance_testing import get_webvitals_script

SITES = {"136": "lazy-local", "1294": "external-ad", "474": "reproduced-ctrl"}
MODEL = "closed_cc-opus-4.6"
DUMP = V.ROOT / "final_result_dumps/main_bench_rerun_20260615" / MODEL
SETTLE = 5000

def patch_for(sid):
    c = list(DUMP.glob(f"{sid}_*/*.patch"))
    return c[0] if c else None

async def scroll_through(page):
    # Step-scroll to bottom to trigger lazy/IO-observer content, then back to top.
    last = -1
    for _ in range(40):
        h = await page.evaluate("document.body.scrollHeight")
        y = await page.evaluate("window.scrollY + window.innerHeight")
        if y >= h and h == last:
            break
        last = h
        await page.evaluate("window.scrollBy(0, window.innerHeight*0.9)")
        await page.wait_for_timeout(250)
    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    await page.wait_for_timeout(500)
    await page.evaluate("window.scrollTo(0, 0)")
    await page.wait_for_timeout(300)

async def measure_once(url, do_scroll):
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        ctx = await b.new_context(viewport={"width": 1500, "height": 800})
        page = await ctx.new_page()
        await page.add_init_script(get_webvitals_script())
        await page.goto(url, wait_until="domcontentloaded", timeout=120000)
        if do_scroll:
            await scroll_through(page)
        await page.wait_for_timeout(SETTLE)
        cls = await page.evaluate("window.__webVitals ? window.__webVitals.cls : null")
        await b.close()
        return round(cls or 0, 4)

def run_site(sid):
    row = V.load_rows()[sid]
    site = {"repo_id": row["REPO_ID"].strip(), "commit": (row.get("COMMIT_ID") or "").strip(),
            "framework": row["FRAMEWORK"].strip(), "host_file": row.get("HOST_FILE_PATH")}
    d = V.reconstruct_site(site, patch_file=patch_for(sid))
    try:
        with V.HostedSite(d, site["framework"], site["host_file"]) as h:
            ns = [asyncio.run(measure_once(h.url, False)) for _ in range(3)]
            sc = [asyncio.run(measure_once(h.url, True)) for _ in range(3)]
        return ns, sc
    finally:
        shutil.rmtree(d, ignore_errors=True)

def main():
    print("TEST 8 — scroll-to-trigger fix; per-run CLS (3 runs each protocol)\n")
    print(f"{'id':>5} {'kind':<16} {'no_scroll':<22} {'scroll':<22}")
    out = {}
    for sid, kind in SITES.items():
        try:
            ns, sc = run_site(sid)
            out[sid] = {"kind": kind, "no_scroll": ns, "scroll": sc}
            print(f"{sid:>5} {kind:<16} {str(ns):<22} {str(sc):<22}")
        except Exception as e:
            print(f"{sid:>5} {kind:<16} ERROR: {e}")
            out[sid] = {"error": str(e)}
    Path(os.path.dirname(__file__), "test8_scroll_fix.json").write_text(json.dumps(out, indent=2))
    print("\nReading: if 'scroll' reproduces the recorded shift consistently while "
          "'no_scroll' stays ~0, the missing-scroll protocol is the determinism bug for "
          "lazy-load CLS. External-driven sites need hermetic freeze in addition.")

if __name__ == "__main__":
    main()
