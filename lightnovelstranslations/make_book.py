#!/usr/bin/env python3
"""
End-to-end pipeline for lightnovelstranslations.com:
TOC URL → chapter URLs → chapter Markdown → EPUB.

Imports the work-horse functions from extract_urls.py, fetch_chapters.py,
and build_epub.py — all four files must sit in the same directory.

Usage:
    # The whole novel in one go
    python make_book.py \\
        --toc-url "https://lightnovelstranslations.com/novel/<slug>/" \\
        --workdir ./novel_build \\
        --epub novel.epub

    # Title / author auto-detected; cover may be missing on some novels and
    # falls back to no-cover. Override anything explicitly:
    python make_book.py --toc-url "..." --epub out.epub \\
        --title "My Title" --author "Real Name" --cover ./mycover.jpg

    # Resume a partial run (idempotent — chapters that already exist are skipped):
    python make_book.py --toc-url "..." --workdir ./novel_build --epub out.epub

    # Build EPUB only, from chapters you already fetched:
    python make_book.py --skip-extract --skip-fetch \\
        --workdir ./novel_build --epub out.epub --title "..." --author "..."
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
from extract_urls import extract as extract_chapter_urls, fetch_chapter_list_html
from fetch_chapters import (
    extract_chapter, filename_for, fetch as fetch_page,
    format_output, UA,
)
from build_epub import build_epub


# ---------------------------------------------------------------------------
# Metadata scraping from the novel's TOC page
# ---------------------------------------------------------------------------

# Theme placeholder shown when a novel has no uploaded cover image
PLACEHOLDER_IMAGES = ("no-image.png", "no_image.png", "noimage.png", "default.jpg")
# Theme decoration / icon images we don't want as covers either
THEME_ASSET_PATTERNS = (
    "/themes/light_novel/",  # any theme asset folder
    "krystal-pack",          # decoration packs
    "/logo.png", "/logo.jpg",
    "night-mode-logo",
)


def parse_detail_info(text):
    """Split '.novel_detail_info' text into a dict of fields.

    The site renders detail info as a flat string like:
        "Author: あまうい白一 Translator: Weslykan Editor: Weslykan
         Raw: http://ncode.syosetu.com/n5915da/ Schedule: Completed"

    so we look for known field labels and capture the text between them.
    """
    labels = ("Author", "Translator", "Editor", "Artist", "Raw",
              "Schedule", "Original", "Published", "Status")
    pattern = re.compile(
        r"(" + "|".join(labels) + r")\s*:\s*(.+?)"
        r"(?=\s+(?:" + "|".join(labels) + r")\s*:|$)",
        re.IGNORECASE | re.DOTALL,
    )
    info = {}
    for m in pattern.finditer(text):
        key = m.group(1).strip().lower()
        val = re.sub(r"\s+", " ", m.group(2)).strip()
        info[key] = val
    return info


def is_real_cover(src):
    """True if the image URL looks like a real uploaded cover, not a theme
    asset or the no-image.png placeholder."""
    if not src:
        return False
    s = src.lower()
    if any(p in s for p in PLACEHOLDER_IMAGES):
        return False
    if any(p in s for p in THEME_ASSET_PATTERNS):
        return False
    # Real covers on WordPress sites live under wp-content/uploads
    return "wp-content/uploads" in s


def scrape_toc_metadata(html_text):
    """Pull title / cover / author / description from a lightnovelstranslations
    novel page. The theme has no Open Graph tags, so we go through class hooks.
    """
    soup = BeautifulSoup(html_text, "html.parser")

    # Title
    title = None
    el = soup.select_one(".novel_title")
    if el:
        title = el.get_text(" ", strip=True)
    if not title and soup.title and soup.title.string:
        title = soup.title.string.strip()

    # Cover: try uploaded images, skipping the theme placeholder
    cover = None
    for img in soup.find_all("img"):
        src = (img.get("data-src") or img.get("src") or "").strip()
        if is_real_cover(src):
            cover = src
            break
    if cover:
        # Strip WP size suffix to get the original
        cover = re.sub(r"-\d+x\d+(\.[a-z]+)$", r"\1", cover, flags=re.I)

    # Author / translator / etc. from .novel_detail_info
    author = None
    description = None
    detail_el = soup.select_one(".novel_detail_info")
    if detail_el:
        info = parse_detail_info(detail_el.get_text(" ", strip=True))
        author = info.get("author")

    # Description: the synopsis lives in .novel_text (or .novel_tab_content)
    desc_el = (soup.select_one(".novel_text")
               or soup.select_one(".novel_tab_content"))
    if desc_el:
        description = desc_el.get_text(" ", strip=True)
        # The theme prepends tab labels to the synopsis text in some cases
        description = re.sub(
            r"^(About|Table of Contents|NOVEL ILLUSTRATIONS?)\s*", "",
            description, flags=re.IGNORECASE,
        ).strip()

    return {"title": title, "cover_url": cover,
            "author": author, "description": description}


def download_cover(url, dest_path, session):
    """Download a cover image to dest_path. Returns the path on success."""
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

def step_extract_urls(toc_url, urls_file):
    """Step 1: scrape the chapter list, write URLs in reading order."""
    print(f"[1/3] Fetching chapter list: {toc_url}", file=sys.stderr)
    html_text = fetch_chapter_list_html(toc_url)
    chapters = extract_chapter_urls(html_text)
    if not chapters:
        sys.exit("Could not find any chapter links.")
    free = [c for c in chapters if not c["is_locked"]]
    n_locked = len(chapters) - len(free)
    urls_file.write_text("\n".join(c["url"] for c in free) + "\n",
                         encoding="utf-8")
    note = f"      Found {len(chapters)} chapters → {urls_file}"
    if n_locked:
        note += f" ({n_locked} locked, skipped)"
    print(note, file=sys.stderr)
    # (url, title-for-heading, position) — position is 1-based reading-order index.
    # For chapters with a label like "Chapter 33.5", we render the heading
    # as the full raw title which is also what the user sees in the chapter list.
    return [(c["url"], c["raw_title"], c["position"]) for c in free]


def step_fetch_chapters(chapter_meta, chapters_dir, session, delay, overwrite):
    """Step 2: fetch each chapter, save as chapter-NNN.md."""
    print(f"[2/3] Fetching {len(chapter_meta)} chapters → {chapters_dir}/",
          file=sys.stderr)
    chapters_dir.mkdir(parents=True, exist_ok=True)

    fetched = skipped = 0
    failed = []

    for i, (url, toc_title, position) in enumerate(chapter_meta, 1):
        path = chapters_dir / filename_for(url, position=position, fmt="md")
        if not overwrite and path.exists():
            print(f"  [{i:>3}/{len(chapter_meta)}] skip: {path.name}",
                  file=sys.stderr)
            skipped += 1
            continue

        try:
            page_html = fetch_page(url, session)
            ch = extract_chapter(page_html, "md")
        except Exception as e:
            print(f"  [{i:>3}/{len(chapter_meta)}] FAIL {url}: {e}",
                  file=sys.stderr)
            failed.append(url)
            continue

        # Prefer the title from the chapter list — it's what the translator
        # used in the index, identical formatting across all chapters.
        if toc_title:
            ch["title"] = toc_title

        text = format_output(ch, "md")
        path.write_text(text, encoding="utf-8")
        print(f"  [{i:>3}/{len(chapter_meta)}] {ch.get('label') or 'Chapter'}  →  {path.name}",
              file=sys.stderr)
        fetched += 1
        if i < len(chapter_meta):
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
    p.add_argument("--toc-url", help="URL of the novel's main page.")
    p.add_argument("--workdir", type=Path, default=Path("./book"),
                   help="Working directory (urls.txt, chapters/, cover image).")
    p.add_argument("--epub", "-o", type=Path, required=True,
                   help="Output .epub path.")

    p.add_argument("--title", help="Book title (auto-detected if omitted).")
    p.add_argument("--author", help="Book author (auto-detected if omitted).")
    p.add_argument("--language", default="en")
    p.add_argument("--identifier", help="Unique book ID (defaults to URL slug).")
    p.add_argument("--description", help="Book description.")
    p.add_argument("--cover", type=Path,
                   help="Local cover image (skips auto-download). Some novels "
                        "on this site have no cover image at all; use this to "
                        "supply your own.")

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
    chapter_meta = None  # list of (url, title, position)

    # ---- Step 1 -----------------------------------------------------------
    if args.skip_extract:
        if not urls_file.exists():
            sys.exit(f"--skip-extract set but {urls_file} does not exist.")
        urls = [u.strip() for u in urls_file.read_text(encoding="utf-8").splitlines()
                if u.strip() and not u.startswith("#")]
        chapter_meta = [(u, "", i) for i, u in enumerate(urls, 1)]
        print(f"[1/3] Skipped. Using {len(urls)} URLs from {urls_file}",
              file=sys.stderr)
    else:
        chapter_meta = step_extract_urls(args.toc_url, urls_file)

    # ---- Step 2 -----------------------------------------------------------
    if args.skip_fetch:
        existing = sorted(chapters_dir.glob("chapter-*.md")) if chapters_dir.exists() else []
        if not existing:
            sys.exit(f"--skip-fetch set but no chapter files in {chapters_dir}.")
        print(f"[2/3] Skipped. Using {len(existing)} existing chapter files.",
              file=sys.stderr)
    else:
        step_fetch_chapters(chapter_meta, chapters_dir, session,
                            args.delay, args.overwrite)

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
            print(f"  warn: couldn't refetch TOC for metadata: {e}",
                  file=sys.stderr)
            toc_html = ""
        meta = scrape_toc_metadata(toc_html) if toc_html else {}
        title = title or meta.get("title")
        author = author or meta.get("author")
        description = description or meta.get("description")
        cover_url = meta.get("cover_url")

    title = title or "Untitled"
    author = author or "Unknown"
    identifier = args.identifier or (
        urlparse(args.toc_url).path.strip("/").split("/")[-1] if args.toc_url else None
    )

    cover_path = args.cover
    if not cover_path and cover_url:
        candidate = workdir / ("cover" + (Path(urlparse(cover_url).path).suffix or ".jpg"))
        cover_path = download_cover(cover_url, candidate, session)
    if not cover_path:
        print("  note: no cover image found (this novel may not have one). "
              "Pass --cover path/to/your.jpg if you'd like to add one.",
              file=sys.stderr)

    # ---- Step 3 -----------------------------------------------------------
    step_build_epub(
        chapters_dir=chapters_dir, out_epub=args.epub, title=title,
        author=author, language=args.language, identifier=identifier,
        cover_path=cover_path, description=description,
    )

    print(f"\nDone. {args.epub}", file=sys.stderr)


if __name__ == "__main__":
    main()
