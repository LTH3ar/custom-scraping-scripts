#!/usr/bin/env python3
"""
End-to-end pipeline: TOC URL  →  chapter URLs  →  chapter Markdown  →  EPUB.

Imports the work-horse functions from extract_urls.py, fetch_chapters.py, and
build_epub.py — all four files must sit in the same directory.

Usage:
    # The whole novel in one go
    python make_book.py \\
        --toc-url "https://curspe.com/novels/<slug>/" \\
        --workdir ./novel_build \\
        --epub novel.epub

    # Author / title can be auto-detected from the TOC page; override if you want
    python make_book.py --toc-url "..." --epub out.epub --title "My Title" --author "KAZU"

    # Resume: re-run the same command. Chapters that already exist are skipped.
    # Force-rebuild: --overwrite

    # Build EPUB only, from chapters you fetched earlier:
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
from extract_urls import extract as extract_chapter_urls
from fetch_chapters import extract_chapter, filename_for, fetch as fetch_page, UA
from build_epub import build_epub


# ---------------------------------------------------------------------------
# Metadata scraping from the TOC page
# ---------------------------------------------------------------------------

def scrape_toc_metadata(html_text):
    """Pull title, cover image URL, and (if present) author from a novel TOC page.

    Title detection order, most reliable first:
      1. <h1 class="wn-title">  — the theme's dedicated novel-title element.
      2. <title> tag, split on " – " / " | " / " - " to drop the site suffix.
      3. og:title / twitter:title meta tags.
      4. First <h1> on the page (often the site logo, hence last resort).
    """
    soup = BeautifulSoup(html_text, "html.parser")

    def meta(prop):
        tag = (soup.find("meta", property=prop)
               or soup.find("meta", attrs={"name": prop}))
        return tag["content"].strip() if tag and tag.get("content") else None

    # 1. Theme-specific h1 used on this site for the novel title.
    title = None
    wn = soup.find("h1", class_=re.compile(r"\bwn-title\b"))
    if wn:
        title = wn.get_text(strip=True)

    # 2. <title> tag, e.g. "The Foo Novel – Curspe" → "The Foo Novel"
    if not title and soup.title and soup.title.string:
        raw = soup.title.string.strip()
        # Split on the rightmost separator and drop the trailing site name.
        for sep in (" – ", " — ", " | ", " - "):
            if sep in raw:
                head, tail = raw.rsplit(sep, 1)
                # Heuristic: the suffix is short (a site name), not a long phrase.
                if len(tail) <= 40:
                    title = head.strip()
                    break
        if not title:
            title = raw

    # 3. og:title etc.
    if not title:
        title = meta("og:title") or meta("twitter:title")

    # 4. First h1 — last resort, may be the site logo.
    if not title:
        h1 = soup.find("h1")
        title = h1.get_text(strip=True) if h1 else None

    # Cover image
    cover = meta("og:image") or meta("twitter:image")
    if not cover:
        # WordPress marks the post's featured image with class `wp-post-image`.
        # The first one on the novel page is the book cover; later ones are
        # chapter thumbnails. A bare `wp-content/uploads/*.jpg` regex would
        # hit the site logo, so we anchor on the class.
        img = soup.find("img", class_=re.compile(r"\bwp-post-image\b"))
        if img and img.get("src"):
            cover = img["src"]
    if cover:
        # Strip WordPress's size suffix (e.g. -450x600) to get the original.
        cover = re.sub(r"-\d+x\d+(\.[a-z]+)$", r"\1", cover, flags=re.I)

    author = (meta("author") or meta("article:author")
              or _author_from_jsonld(soup))
    return {"title": title, "cover_url": cover, "author": author}


def _author_from_jsonld(soup):
    import json
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(s.string or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict):
            a = data.get("author")
            if isinstance(a, dict) and a.get("name"):
                return a["name"]
            if isinstance(a, str):
                return a
    return None


def download_cover(url, dest_path, session):
    """Download a cover image to dest_path. Returns the path on success, None on failure."""
    try:
        r = session.get(url, headers={"User-Agent": UA}, timeout=30)
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"  cover download failed: {e}", file=sys.stderr)
        return None
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_bytes(r.content)
    return dest_path


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def write_chapter_file(path, title, body_md):
    path.write_text(f"# {title}\n\n{body_md}", encoding="utf-8")


def step_extract_urls(toc_url, urls_file, session):
    """Step 1: fetch the TOC page, extract chapter URLs, write them to urls_file."""
    print(f"[1/3] Fetching TOC page: {toc_url}", file=sys.stderr)
    html_text = fetch_page(toc_url, session)
    chapters = extract_chapter_urls(html_text)
    if not chapters:
        sys.exit("Could not find any chapter links on the TOC page.")
    urls_file.write_text("\n".join(c["url"] for c in chapters) + "\n", encoding="utf-8")
    print(f"      Found {len(chapters)} chapters → {urls_file}", file=sys.stderr)
    return html_text, chapters


def step_fetch_chapters(urls, chapters_dir, session, delay, overwrite):
    """Step 2: fetch each chapter URL, save as chapter-NNN-*.md."""
    print(f"[2/3] Fetching {len(urls)} chapters → {chapters_dir}/", file=sys.stderr)
    chapters_dir.mkdir(parents=True, exist_ok=True)

    fetched = 0
    skipped = 0
    failed = []

    for i, url in enumerate(urls, 1):
        # Compute filename from URL alone for the skip check
        provisional = chapters_dir / filename_for(url, "", "md")
        if not overwrite and provisional.exists():
            print(f"  [{i:>3}/{len(urls)}] skip (exists): {provisional.name}", file=sys.stderr)
            skipped += 1
            continue

        try:
            page_html = fetch_page(url, session)
            ch = extract_chapter(page_html, "md")
        except Exception as e:
            print(f"  [{i:>3}/{len(urls)}] FAIL {url}: {e}", file=sys.stderr)
            failed.append(url)
            continue

        # Replace provisional name with one based on the real title slug
        target = chapters_dir / filename_for(url, ch["title"], "md")
        write_chapter_file(target, ch["title"], ch["body"])
        print(f"  [{i:>3}/{len(urls)}] {ch['title']}  →  {target.name}", file=sys.stderr)
        fetched += 1
        if i < len(urls):
            time.sleep(delay)

    print(f"      fetched={fetched} skipped={skipped} failed={len(failed)}", file=sys.stderr)
    if failed:
        print("      retry these URLs later:", file=sys.stderr)
        for u in failed:
            print(f"        {u}", file=sys.stderr)
    return fetched, skipped, failed


def step_build_epub(chapters_dir, out_epub, title, author, language,
                    identifier, cover_path, description):
    """Step 3: bundle the markdown chapters into an EPUB."""
    print(f"[3/3] Building EPUB → {out_epub}", file=sys.stderr)
    return build_epub(
        chapters_dir=chapters_dir,
        out_path=out_epub,
        title=title,
        author=author,
        language=language,
        identifier=identifier,
        cover_path=cover_path,
        description=description,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--toc-url", help="URL of the novel's chapter list page.")
    p.add_argument("--workdir", type=Path, default=Path("./book"),
                   help="Working directory (holds urls.txt, chapters/, cover image).")
    p.add_argument("--epub", "-o", type=Path, required=True, help="Output .epub path.")

    p.add_argument("--title", help="Book title (auto-detected if omitted).")
    p.add_argument("--author", help="Book author (auto-detected if omitted, falls back to 'Unknown').")
    p.add_argument("--language", default="en")
    p.add_argument("--identifier", help="Unique book ID (defaults to URL slug).")
    p.add_argument("--description")
    p.add_argument("--cover", type=Path, help="Local cover image (skips auto-download).")

    p.add_argument("--skip-extract", action="store_true",
                   help="Skip step 1: assume workdir/urls.txt already exists.")
    p.add_argument("--skip-fetch", action="store_true",
                   help="Skip step 2: assume workdir/chapters/ is already populated.")
    p.add_argument("--skip-epub", action="store_true",
                   help="Skip step 3: don't build the EPUB.")
    p.add_argument("--overwrite", action="store_true", help="Re-fetch chapters that already exist.")
    p.add_argument("--delay", type=float, default=1.0, help="Seconds between requests.")
    args = p.parse_args()

    if not (args.skip_extract and args.skip_fetch) and not args.toc_url:
        p.error("--toc-url is required unless both --skip-extract and --skip-fetch are set.")

    workdir = args.workdir
    workdir.mkdir(parents=True, exist_ok=True)
    urls_file = workdir / "urls.txt"
    chapters_dir = workdir / "chapters"

    session = requests.Session()
    toc_html = None

    # ---- Step 1: extract URLs ---------------------------------------------
    if args.skip_extract:
        if not urls_file.exists():
            sys.exit(f"--skip-extract set but {urls_file} does not exist.")
        urls = [u.strip() for u in urls_file.read_text(encoding="utf-8").splitlines()
                if u.strip() and not u.startswith("#")]
        print(f"[1/3] Skipped. Using {len(urls)} URLs from {urls_file}", file=sys.stderr)
    else:
        toc_html, chapters = step_extract_urls(args.toc_url, urls_file, session)
        urls = [c["url"] for c in chapters]

    # ---- Step 2: fetch chapters -------------------------------------------
    if args.skip_fetch:
        existing = sorted(chapters_dir.glob("chapter-*.md")) if chapters_dir.exists() else []
        if not existing:
            sys.exit(f"--skip-fetch set but no chapter-*.md files found in {chapters_dir}.")
        print(f"[2/3] Skipped. Using {len(existing)} existing chapter files.", file=sys.stderr)
    else:
        step_fetch_chapters(urls, chapters_dir, session, args.delay, args.overwrite)

    if args.skip_epub:
        print("[3/3] Skipped (--skip-epub).", file=sys.stderr)
        return

    # ---- Metadata for EPUB ------------------------------------------------
    title, author, cover_url = args.title, args.author, None
    if (not title or not author or not args.cover) and args.toc_url:
        if toc_html is None:
            try:
                toc_html = fetch_page(args.toc_url, session)
            except Exception as e:
                print(f"  warn: couldn't refetch TOC for metadata: {e}", file=sys.stderr)
                toc_html = ""
        meta = scrape_toc_metadata(toc_html) if toc_html else {}
        title = title or meta.get("title")
        author = author or meta.get("author")
        cover_url = meta.get("cover_url")

    title = title or "Untitled"
    author = author or "Unknown"
    identifier = args.identifier or (
        urlparse(args.toc_url).path.strip("/").split("/")[-1] if args.toc_url else None
    )

    # Cover handling
    cover_path = args.cover
    if not cover_path and cover_url:
        candidate = workdir / ("cover" + (Path(urlparse(cover_url).path).suffix or ".jpg"))
        cover_path = download_cover(cover_url, candidate, session)

    # ---- Step 3: build EPUB -----------------------------------------------
    step_build_epub(
        chapters_dir=chapters_dir,
        out_epub=args.epub,
        title=title,
        author=author,
        language=args.language,
        identifier=identifier,
        cover_path=cover_path,
        description=args.description,
    )

    print(f"\nDone. {args.epub}", file=sys.stderr)


if __name__ == "__main__":
    main()
