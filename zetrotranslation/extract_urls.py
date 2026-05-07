#!/usr/bin/env python3
"""
Extract chapter URLs from a wp-manga themed novel page (zetrotranslation.com
and many similar WordPress translation sites).

Each chapter is rendered as:
    <li class="wp-manga-chapter ...">
        <span class="coin">Free</span> | <span class="coin">Premium</span> ...
        <a href="...">126 - Title</a>
        <span class="chapter-release-date"><i>2025-06-28</i></span>
        <span class="view"><i></i> 2335</span>
    </li>

Title text comes in three shapes:
    "126 - Title"                  single chapter
    "124 END - Title"              chapter with a suffix tag
    "122-123 - Title1 || Title2"   two chapters merged into one post

Usage:
    # From a saved HTML file:
    python extract_urls.py page.html

    # Directly from the live page (requires `requests`):
    python extract_urls.py --url https://zetrotranslation.com/novel/<slug>/

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


# Title-text parser. Anchored on the leading number(s):
#   group 1: first chapter number
#   group 2: optional `-N` second number for ranges (e.g. 122-123)
#   group 3: optional suffix word (END, EXTRA, etc.) — uppercase
#   group 4: rest of the title after the " - " separator
TITLE_RE = re.compile(
    r"""
    ^\s*
    (\d+)                  # leading chapter number
    (?:-(\d+))?            # optional second number for ranges
    (?:\s+([A-Z][A-Z]+))?  # optional suffix tag like END, EXTRA
    \s*-\s*                # separator between number and title
    (.+?)                  # title (greedy-min, trimmed)
    \s*$
    """,
    re.VERBOSE,
)


def parse_title(raw):
    """Split a chapter list label into (num, num_end, suffix, clean_title)."""
    raw = re.sub(r"\s+", " ", raw).strip()
    m = TITLE_RE.match(raw)
    if not m:
        return None, None, "", raw
    num = int(m.group(1))
    num_end = int(m.group(2)) if m.group(2) else num
    suffix = m.group(3) or ""
    title = m.group(4).strip()
    # "Title1 || Title2" → "Title1 / Title2" for nicer display in EPUBs/filenames
    title = re.sub(r"\s*\|\|\s*", " / ", title)
    return num, num_end, suffix, title


def extract(html):
    """Return a list of chapter dicts, sorted ascending by chapter number."""
    soup = BeautifulSoup(html, "html.parser")
    chapters = []
    for li in soup.select("li.wp-manga-chapter"):
        a = li.find("a", href=True)
        if not a:
            continue
        url = a["href"].strip()
        raw_title = a.get_text(" ", strip=True)
        num, num_end, suffix, title = parse_title(raw_title)

        date_el = li.select_one(".chapter-release-date i, .chapter-release-date")
        date = date_el.get_text(strip=True) if date_el else ""

        coin_el = li.select_one(".coin")
        coin = coin_el.get_text(strip=True) if coin_el else ""
        # `free-chap` / `premium-chap` etc. are also encoded in the <li> classes
        is_locked = bool(coin and coin.lower() != "free") or any(
            "premium" in c.lower() or "paid" in c.lower() or "lock" in c.lower()
            for c in li.get("class", [])
        )

        view_el = li.select_one(".view")
        views = None
        if view_el:
            m = re.search(r"\d[\d,]*", view_el.get_text(" ", strip=True))
            if m:
                views = int(m.group(0).replace(",", ""))

        chapters.append({
            "num": num,
            "num_end": num_end,
            "suffix": suffix,
            "title": title,
            "raw_title": raw_title,
            "url": url,
            "date": date,
            "coin": coin,
            "is_locked": is_locked,
            "views": views,
        })

    # Sort ascending by (first number, last number) so 122-123 comes between
    # 122 and 124, and entries without numbers go last.
    chapters.sort(key=lambda c: (c["num"] is None, c["num"] or 0, c["num_end"] or 0))
    return chapters


def fetch_chapter_list_html(toc_url):
    """wp-manga loads chapters via AJAX, so a plain GET on the novel page
    returns an empty list. We:
      1. GET the novel page to scrape its manga ID (data-id="…").
      2. POST to /wp-admin/admin-ajax.php with action=manga_get_chapters
         to get the chapter-list HTML fragment.
    """
    try:
        import requests
    except ImportError:
        sys.exit("`requests` not installed. Run: pip install requests")
    from urllib.parse import urlparse

    headers = {"User-Agent": "Mozilla/5.0 (chapter-extractor)"}
    sess = requests.Session()

    page = sess.get(toc_url, headers=headers, timeout=30)
    page.raise_for_status()

    m = (re.search(r'data-id=["\'](\d+)["\']', page.text)
         or re.search(r'manga_id["\']?\s*[:=]\s*["\']?(\d+)', page.text))
    if not m:
        # Fall back to whatever was in the page directly. Some sites do render
        # the list server-side; only the AJAX-loaded ones need step 2.
        return page.text
    manga_id = m.group(1)

    parsed = urlparse(toc_url)
    ajax_url = f"{parsed.scheme}://{parsed.netloc}/wp-admin/admin-ajax.php"
    r = sess.post(
        ajax_url,
        data={"action": "manga_get_chapters", "manga": manga_id},
        headers={**headers, "X-Requested-With": "XMLHttpRequest", "Referer": toc_url},
        timeout=30,
    )
    r.raise_for_status()
    return r.text


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
                     help="Output TSV: num<TAB>title<TAB>url<TAB>date<TAB>locked<TAB>views")
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
                f"{c['num'] or ''}\t{c['title']}\t{c['url']}\t{c['date']}"
                f"\t{'locked' if c['is_locked'] else 'free'}\t{c['views'] or ''}\n"
            )
    else:
        for c in chapters:
            if c["is_locked"] and not args.include_locked:
                continue
            sys.stdout.write(c["url"] + "\n")

    n_locked = sum(1 for c in chapters if c["is_locked"])
    msg = f"Extracted {len(chapters)} chapters"
    if n_locked:
        msg += f" ({n_locked} locked, skipped)" if not args.include_locked else f" ({n_locked} locked, included)"
    sys.stderr.write(msg + ".\n")


if __name__ == "__main__":
    main()
