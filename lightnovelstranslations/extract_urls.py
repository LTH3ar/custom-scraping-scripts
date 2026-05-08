#!/usr/bin/env python3
"""
Extract chapter URLs from a lightnovelstranslations.com novel page.

The TOC is rendered server-side under `?tab=table_contents`. Chapters are
grouped into accordion sections by 50s ("Chapter 1-50", "Chapter 51-100",
…). Within sections the order isn't always strictly numeric — the site
sometimes lists 100.5 before 100, or 252 before 251 — so we treat
DOM order as the reading order and use a global position counter to sort.

Title-text shapes handled:
    Prologue: <title>
    Chapter 1: <title>
    Chapter 33.5: <title>            # decimal/side
    Chapter 250-1: <title>           # split chapters
    Side Chapter 1: <title>

Locked chapters: rows with class `unlock` are free; rows lacking it are
treated as locked and skipped from the URL output by default.

Usage:
    # From a saved HTML file:
    python extract_urls.py page.html

    # Live (the script appends ?tab=table_contents if missing):
    python extract_urls.py --url https://lightnovelstranslations.com/novel/<slug>/

    # Full metadata:
    python extract_urls.py page.html --tsv > chapters.tsv
    python extract_urls.py page.html --json > chapters.json
"""

import argparse
import json
import re
import sys

try:
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("Need BeautifulSoup. Run: pip install beautifulsoup4")


# Title-text parser. Matches:
#   "Prologue: title"
#   "Epilogue: title"
#   "Chapter 33.5: title"
#   "Chapter 250-1: title"
#   "Side Chapter 1: title"
LABEL_RE = re.compile(
    r"""
    ^\s*
    (
        Prologue
      | Epilogue
      | Side\s+Chapter\s+[\d.]+
      | Chapter\s+[\d.\-]+
    )
    \s*[:\-—–]\s*
    (.+?)
    \s*$
    """,
    re.VERBOSE | re.IGNORECASE,
)


def parse_label(text):
    """Split a chapter title into (label, title_only).

    'Chapter 33.5: —Side Dianeia— The People…' →
        ('Chapter 33.5', '—Side Dianeia— The People…')
    """
    text = re.sub(r"\s+", " ", text).strip()
    m = LABEL_RE.match(text)
    if m:
        # Normalise label whitespace ("Chapter   33.5" → "Chapter 33.5")
        label = re.sub(r"\s+", " ", m.group(1)).strip()
        return label, m.group(2).strip()
    return "", text


def fetch_chapter_list_html(toc_url):
    """Fetch the page that contains the chapter list. The site uses
    `?tab=table_contents` as a UI hint; the chapter HTML is the same with
    or without the parameter, but we add it to be explicit.
    """
    try:
        import requests
    except ImportError:
        sys.exit("`requests` not installed. Run: pip install requests")

    if "tab=" not in toc_url:
        sep = "&" if "?" in toc_url else "?"
        toc_url = toc_url + sep + "tab=table_contents"

    headers = {"User-Agent": "Mozilla/5.0 (chapter-extractor)"}
    r = requests.get(toc_url, headers=headers, timeout=30)
    r.raise_for_status()
    return r.text


def extract(html):
    """Return a list of chapter dicts in reading order (DOM order)."""
    soup = BeautifulSoup(html, "html.parser")
    chapters = []

    # Track which accordion section each chapter belongs to, for debugging
    # / metadata. Walk the full chapter list container so we visit sections
    # in order.
    container = soup.select_one(".novel_list_chapter, .novel_list_chapter_content") or soup
    current_section = ""

    for el in container.find_all(["h3", "li"]):
        if el.name == "h3" and "accordition_item_title" in (el.get("class") or []):
            current_section = el.get_text(" ", strip=True)
            continue
        if el.name != "li":
            continue
        classes = set(el.get("class") or [])
        if "chapter-item" not in classes:
            continue

        a = el.find("a", href=True)
        if not a:
            continue
        url = a["href"].strip()
        # The title attribute is more reliable than inner text — it
        # preserves spacing and punctuation that BS4 would collapse.
        raw_title = (a.get("title") or a.get_text(" ", strip=True)).strip()
        raw_title = re.sub(r"\s+", " ", raw_title)
        label, title_only = parse_label(raw_title)

        # `unlock` class is the explicit marker that the chapter is free.
        # Absence of it usually means premium/locked on this theme.
        is_locked = "unlock" not in classes

        chapters.append({
            "position": len(chapters) + 1,    # 1-based, in reading order
            "section": current_section,
            "url": url,
            "label": label,                    # "Prologue", "Chapter 33.5", …
            "title": title_only,               # text after the colon
            "raw_title": raw_title,            # the full link text
            "is_locked": is_locked,
        })

    return chapters


def load_html(args):
    if args.url:
        return fetch_chapter_list_html(args.url)
    if args.path == "-":
        return sys.stdin.read()
    with open(args.path, "r", encoding="utf-8") as f:
        return f.read()


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("path", nargs="?", default="-",
                   help="HTML file path, or `-` for stdin (default).")
    p.add_argument("--url", help="Fetch the page directly from this URL.")
    fmt = p.add_mutually_exclusive_group()
    fmt.add_argument("--json", action="store_true", help="Output JSON.")
    fmt.add_argument("--tsv", action="store_true",
                     help="Output TSV: position<TAB>label<TAB>title<TAB>url<TAB>section<TAB>locked")
    p.add_argument("--include-locked", action="store_true",
                   help="Include locked/premium chapters in URL output (default: skip).")
    args = p.parse_args()

    html = load_html(args)
    chapters = extract(html)

    if args.json:
        json.dump(chapters, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    elif args.tsv:
        for c in chapters:
            sys.stdout.write(
                f"{c['position']}\t{c['label']}\t{c['title']}\t{c['url']}"
                f"\t{c['section']}\t{'locked' if c['is_locked'] else 'free'}\n"
            )
    else:
        for c in chapters:
            if c["is_locked"] and not args.include_locked:
                continue
            sys.stdout.write(c["url"] + "\n")

    n_locked = sum(1 for c in chapters if c["is_locked"])
    msg = f"Extracted {len(chapters)} chapters"
    if n_locked:
        msg += (f" ({n_locked} locked, skipped)" if not args.include_locked
                else f" ({n_locked} locked, included)")
    sys.stderr.write(msg + ".\n")


if __name__ == "__main__":
    main()
