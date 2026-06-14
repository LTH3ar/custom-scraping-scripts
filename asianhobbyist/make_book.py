#!/usr/bin/env python3
"""
End-to-end pipeline for asianhobbyist.com:
series URL → chapter URLs (with range filter) → chapter Markdown → EPUB.

This site hosts partial fan translations, so chapter numbers have gaps and
two different URL slug schemes. The --from / --to range filter is the main
tool for grabbing just the span you want; URLs are resolved by reading the
"Ch. N" labels off the series page (see extract_urls.py).

Imports the work-horse functions from extract_urls.py, fetch_chapters.py,
and build_epub.py — all four files must sit in the same directory.

Usage:
    # A specific chapter range (inclusive). This is the common case here.
    python make_book.py \\
        --toc-url "https://www.asianhobbyist.com/series/<slug>/" \\
        --from 231 --to 274 \\
        --workdir ./book \\
        --epub novel.epub

    # Everything the site has translated:
    python make_book.py --toc-url "..." --workdir ./book --epub novel.epub

    # Override auto-detected metadata:
    python make_book.py --toc-url "..." --from 231 --to 274 --epub out.epub \\
        --title "..." --author "..." --cover ./mycover.jpg

    # Resume (idempotent): rerun the same command; existing chapters are skipped.
    # Build EPUB only, from chapters already fetched:
    python make_book.py --skip-extract --skip-fetch \\
        --workdir ./book --epub out.epub --title "..."
"""

import argparse
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

# Local module imports — all four scripts must live side-by-side.
from extract_urls import extract as extract_chapter_urls, in_range
from fetch_chapters import (
    extract_chapter, filename_for, fetch as fetch_page,
    format_output, UA,
)
from build_epub import build_epub


# ---------------------------------------------------------------------------
# Metadata scraping from the series page
# ---------------------------------------------------------------------------

def strip_site_suffix(s):
    """Drop a trailing ' – Site Name' / ' | Site Name' when the suffix is
    short enough to plausibly be a site name."""
    if not s:
        return s
    for sep in (" – ", " — ", " | ", " - "):
        if sep in s:
            head, tail = s.rsplit(sep, 1)
            if len(tail) <= 40:
                return head.strip()
    return s


def scrape_series_metadata(html_text):
    """Pull title / cover / author / description from an asianhobbyist series
    page. og:title and og:image are present; description lives in
    `.description`; author is usually absent on these fan-translation pages.
    """
    soup = BeautifulSoup(html_text, "html.parser")

    def meta(prop):
        tag = (soup.find("meta", property=prop)
               or soup.find("meta", attrs={"name": prop}))
        return tag["content"].strip() if tag and tag.get("content") else None

    # Title: prefer the clean page heading, then og:title (strip site suffix)
    title = None
    h1 = soup.select_one("h1.entry-title")
    if h1:
        title = re.sub(r"\s+", " ", h1.get_text(" ", strip=True))
    if not title:
        title = strip_site_suffix(meta("og:title"))
    if not title and soup.title and soup.title.string:
        title = strip_site_suffix(soup.title.string.strip())

    cover = meta("og:image") or meta("twitter:image")
    if cover:
        cover = re.sub(r"\?.*$", "", cover)

    # Author rarely present; look for a labelled hook just in case.
    author = None
    for label in ("Author", "Original Author"):
        node = soup.find(string=re.compile(rf"^\s*{label}\s*:", re.I))
        if node:
            m = re.search(rf"{label}\s*:\s*(.+)", node.strip(), re.I)
            if m:
                author = m.group(1).strip()[:120]
                break

    description = None
    desc_el = soup.select_one(".description, .series-synopsis, .synopsis")
    if desc_el:
        description = desc_el.get_text(" ", strip=True)

    return {"title": title, "cover_url": cover,
            "author": author, "description": description}


def download_cover(url, dest_path, session):
    parsed = urlparse(url)
    referer = f"{parsed.scheme}://{parsed.netloc}/"
    try:
        r = session.get(url, headers={"User-Agent": UA, "Referer": referer},
                        timeout=30)
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"  cover download failed: {e}", file=sys.stderr)
        return None
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_bytes(r.content)
    return dest_path


# ---------------------------------------------------------------------------
# Pipeline steps
# ---------------------------------------------------------------------------

def step_extract_urls(toc_url, urls_file, lo, hi, session):
    """Step 1: scrape the chapter list, filter by range, write URLs."""
    print(f"[1/3] Fetching chapter list: {toc_url}", file=sys.stderr)
    html_text = fetch_page(toc_url, session)
    chapters = extract_chapter_urls(html_text)
    if not chapters:
        sys.exit("Could not find any chapter links on the series page.")
    selected = [c for c in chapters if in_range(c["num"], lo, hi)]
    if not selected:
        sys.exit(f"No chapters in range [{lo}, {hi}]. "
                 f"Site has {chapters[0]['label']}..{chapters[-1]['label']}.")

    urls_file.write_text("\n".join(c["url"] for c in selected) + "\n",
                         encoding="utf-8")

    note = f"      Found {len(chapters)} chapters total; {len(selected)} selected"
    if lo is not None or hi is not None:
        lo_d = lo if lo is not None else chapters[0]["num"]
        hi_d = hi if hi is not None else chapters[-1]["num"]
        note += f" in range [{lo_d:g}, {hi_d:g}]"
        present_ints = {int(c["num"]) for c in selected}
        missing = [n for n in range(int(lo_d), int(hi_d) + 1) if n not in present_ints]
        if missing:
            runs = []
            s = e = missing[0]
            for n in missing[1:]:
                if n == e + 1:
                    e = n
                else:
                    runs.append((s, e)); s = e = n
            runs.append((s, e))
            pretty = ", ".join(f"{a}" if a == b else f"{a}-{b}" for a, b in runs)
            note += f"; gaps (untranslated/absent): {pretty}"
    note += f" → {urls_file}"
    print(note, file=sys.stderr)
    return [c["url"] for c in selected]


def step_fetch_chapters(urls, chapters_dir, session, delay, overwrite):
    """Step 2: fetch each chapter, save as chapter-<num>.md."""
    print(f"[2/3] Fetching {len(urls)} chapters → {chapters_dir}/", file=sys.stderr)
    chapters_dir.mkdir(parents=True, exist_ok=True)

    fetched = skipped = 0
    failed = []

    for i, url in enumerate(urls, 1):
        # The chapter number (and thus filename) isn't known until we parse
        # the page, so for the skip check we fetch first, then decide.
        # To honour --overwrite=False efficiently we peek at a slug-based
        # name; but since slugs are inconsistent we just fetch and compare.
        try:
            page_html = fetch_page(url, session)
            ch = extract_chapter(page_html, "md")
        except Exception as e:
            print(f"  [{i:>3}/{len(urls)}] FAIL {url}: {e}", file=sys.stderr)
            failed.append(url)
            continue

        path = chapters_dir / filename_for(url, number=ch["number"], fmt="md")
        if not overwrite and path.exists():
            print(f"  [{i:>3}/{len(urls)}] skip: {path.name}", file=sys.stderr)
            skipped += 1
            continue

        text = format_output(ch, "md")
        path.write_text(text, encoding="utf-8")
        print(f"  [{i:>3}/{len(urls)}] Ch. {ch['number']}  →  {path.name}",
              file=sys.stderr)
        fetched += 1
        if i < len(urls):
            time.sleep(delay)

    print(f"      fetched={fetched} skipped={skipped} failed={len(failed)}",
          file=sys.stderr)
    if failed:
        print("      retry these later:", file=sys.stderr)
        for u in failed:
            print(f"        {u}", file=sys.stderr)
    return fetched, skipped, failed


def step_build_epub(chapters_dir, out_epub, title, author, language,
                    identifier, cover_path, description):
    print(f"[3/3] Building EPUB → {out_epub}", file=sys.stderr)
    return build_epub(
        chapters_dir=chapters_dir, out_path=out_epub, title=title,
        author=author, language=language, identifier=identifier,
        cover_path=cover_path, description=description,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--toc-url", help="URL of the novel's series page.")
    p.add_argument("--from", dest="lo", type=float, default=None,
                   help="Lowest chapter number to include (inclusive).")
    p.add_argument("--to", dest="hi", type=float, default=None,
                   help="Highest chapter number to include (inclusive).")
    p.add_argument("--workdir", type=Path, default=Path("./book"),
                   help="Working directory (urls.txt, chapters/, cover image).")
    p.add_argument("--epub", "-o", type=Path, required=True,
                   help="Output .epub path.")

    p.add_argument("--title", help="Book title (auto-detected if omitted).")
    p.add_argument("--author", help="Book author (auto-detected if omitted).")
    p.add_argument("--language", default="en")
    p.add_argument("--identifier", help="Unique book ID (defaults to URL slug + range).")
    p.add_argument("--description", help="Book description.")
    p.add_argument("--cover", type=Path, help="Local cover image (skips auto-download).")

    p.add_argument("--skip-extract", action="store_true",
                   help="Skip step 1: assume workdir/urls.txt already exists.")
    p.add_argument("--skip-fetch", action="store_true",
                   help="Skip step 2: assume workdir/chapters/ is populated.")
    p.add_argument("--skip-epub", action="store_true",
                   help="Skip step 3: don't build the EPUB.")
    p.add_argument("--overwrite", action="store_true",
                   help="Re-fetch chapters that already exist.")
    p.add_argument("--delay", type=float, default=1.0,
                   help="Seconds between chapter fetches.")
    args = p.parse_args()

    if not (args.skip_extract and args.skip_fetch) and not args.toc_url:
        p.error("--toc-url is required unless both --skip-extract and "
                "--skip-fetch are given.")

    workdir = args.workdir
    workdir.mkdir(parents=True, exist_ok=True)
    urls_file = workdir / "urls.txt"
    chapters_dir = workdir / "chapters"

    session = requests.Session()

    # ---- Step 1 -----------------------------------------------------------
    if args.skip_extract:
        if not urls_file.exists():
            sys.exit(f"--skip-extract set but {urls_file} does not exist.")
        urls = [u.strip() for u in urls_file.read_text(encoding="utf-8").splitlines()
                if u.strip() and not u.startswith("#")]
        print(f"[1/3] Skipped. Using {len(urls)} URLs from {urls_file}",
              file=sys.stderr)
    else:
        urls = step_extract_urls(args.toc_url, urls_file, args.lo, args.hi, session)

    # ---- Step 2 -----------------------------------------------------------
    if args.skip_fetch:
        existing = sorted(chapters_dir.glob("chapter-*.md")) if chapters_dir.exists() else []
        if not existing:
            sys.exit(f"--skip-fetch set but no chapter files in {chapters_dir}.")
        print(f"[2/3] Skipped. Using {len(existing)} existing chapter files.",
              file=sys.stderr)
    else:
        step_fetch_chapters(urls, chapters_dir, session, args.delay, args.overwrite)

    if args.skip_epub:
        print("[3/3] Skipped (--skip-epub).", file=sys.stderr)
        return

    # ---- metadata for EPUB ------------------------------------------------
    title, author, description = args.title, args.author, args.description
    cover_url = None
    if (not title or not author or not description or not args.cover) and args.toc_url:
        try:
            toc_html = fetch_page(args.toc_url, session)
        except Exception as e:
            print(f"  warn: couldn't refetch series page for metadata: {e}",
                  file=sys.stderr)
            toc_html = ""
        meta = scrape_series_metadata(toc_html) if toc_html else {}
        title = title or meta.get("title")
        author = author or meta.get("author")
        description = description or meta.get("description")
        cover_url = meta.get("cover_url")

    # Annotate the title with the chapter range so a partial book is
    # self-describing in the library (e.g. "... (Ch. 231-274)").
    base_title = title or "Untitled"
    if (args.lo is not None or args.hi is not None) and not args.title:
        lo_s = f"{args.lo:g}" if args.lo is not None else "start"
        hi_s = f"{args.hi:g}" if args.hi is not None else "end"
        title = f"{base_title} (Ch. {lo_s}-{hi_s})"
    else:
        title = base_title

    author = author or "Unknown"

    identifier = args.identifier
    if not identifier and args.toc_url:
        slug = urlparse(args.toc_url).path.strip("/").split("/")[-1]
        rng = ""
        if args.lo is not None or args.hi is not None:
            rng = f"-{args.lo:g}-{args.hi:g}" if args.lo is not None and args.hi is not None else "-partial"
        identifier = slug + rng

    cover_path = args.cover
    if not cover_path and cover_url:
        candidate = workdir / ("cover" + (Path(urlparse(cover_url).path).suffix or ".jpg"))
        cover_path = download_cover(cover_url, candidate, session)
    if not cover_path:
        print("  note: no cover image found. Pass --cover path/to/your.jpg "
              "to add one.", file=sys.stderr)

    # ---- Step 3 -----------------------------------------------------------
    step_build_epub(
        chapters_dir=chapters_dir, out_epub=args.epub, title=title,
        author=author, language=args.language, identifier=identifier,
        cover_path=cover_path, description=description,
    )

    print(f"\nDone. {args.epub}", file=sys.stderr)


if __name__ == "__main__":
    main()