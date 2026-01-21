#!/usr/bin/env python3
"""Fetch sitemap.xml and extract URLs into a plain text file.

Usage:
    python scripts/helper_scripts/crawling/fetch_sitemap.py --sitemap https://example.com/sitemap.xml --out urls.txt
"""
import argparse
import sys
from pathlib import Path
import requests


def fetch_sitemap(sitemap_url: str) -> list:
    r = requests.get(sitemap_url, timeout=15)
    r.raise_for_status()
    text = r.text
    # crude extraction of <loc>...</loc>
    import re
    return re.findall(r"<loc>(.*?)</loc>", text)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--sitemap", required=True, help="Sitemap URL")
    p.add_argument("--out", default="urls.txt", help="Output file")
    args = p.parse_args()

    urls = fetch_sitemap(args.sitemap)
    out = Path(args.out)
    out.write_text("\n".join(urls))
    print(f"Wrote {len(urls)} URLs to {out}")


if __name__ == "__main__":
    main()
