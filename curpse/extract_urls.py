#!/usr/bin/env python3
"""
Extract chapter URLs (and optionally titles/word counts) from the chapter
list HTML on curspe.com novel pages.

Usage:
    # From a saved HTML file:
    python extract_urls.py path/to/page.html

    # Directly from the live page (requires `requests`):
    python extract_urls.py --url https://curspe.com/novels/<slug>/

    # Output as TSV with title + word count:
    python extract_urls.py page.html --tsv > chapters.tsv

    # Output as JSON:
    python extract_urls.py page.html --json > chapters.json
"""

import argparse
import json
import re
import sys
from html import unescape


# Each chapter is rendered as:
#   <a href="..." class="wn-chapter-item" data-locked="0" data-title="..." ...>
#       ...<h4 class="wn-chapter-title">Chapter N – ...</h4>...
#       ...<span>Mar 3, 2026</span> ... <span>1,875 words</span>...
#   </a>
CHAPTER_BLOCK_RE = re.compile(
    r'<a\s+href="(?P<url>[^"]+)"[^>]*class="wn-chapter-item"[^>]*>'
    r'(?P<inner>.*?)</a>',
    re.DOTALL,
)
TITLE_RE = re.compile(
    r'<h4\s+class="wn-chapter-title">(?P<title>.*?)</h4>', re.DOTALL
)
WORDS_RE = re.compile(r'<span>([\d,]+)\s+words?</span>', re.IGNORECASE)
DATE_RE = re.compile(
    r'<span>([A-Z][a-z]{2}\s+\d{1,2},\s+\d{4})</span>'
)
CHAPTER_NUM_RE = re.compile(r'Chapter\s+(\d+)', re.IGNORECASE)


def extract(html):
    """Return a list of dicts: {num, title, url, date, words}."""
    chapters = []
    for m in CHAPTER_BLOCK_RE.finditer(html):
        url = unescape(m.group('url')).strip()
        inner = m.group('inner')

        title_m = TITLE_RE.search(inner)
        title = unescape(title_m.group('title')).strip() if title_m else ''

        num_m = CHAPTER_NUM_RE.search(title)
        num = int(num_m.group(1)) if num_m else None

        date_m = DATE_RE.search(inner)
        date = date_m.group(1) if date_m else ''

        words_m = WORDS_RE.search(inner)
        words = int(words_m.group(1).replace(',', '')) if words_m else None

        chapters.append({
            'num': num,
            'title': title,
            'url': url,
            'date': date,
            'words': words,
        })

    # Sort by chapter number when available so output is deterministic
    chapters.sort(key=lambda c: (c['num'] is None, c['num'] or 0))
    return chapters


def load_html(args):
    if args.url:
        try:
            import requests
        except ImportError:
            sys.exit("`requests` not installed. Run: pip install requests")
        headers = {'User-Agent': 'Mozilla/5.0 (chapter-url-extractor)'}
        r = requests.get(args.url, headers=headers, timeout=30)
        r.raise_for_status()
        return r.text
    if args.path == '-':
        return sys.stdin.read()
    with open(args.path, 'r', encoding='utf-8') as f:
        return f.read()


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('path', nargs='?', default='-',
                   help='HTML file path, or `-` for stdin (default).')
    p.add_argument('--url', help='Fetch the page directly from this URL.')
    fmt = p.add_mutually_exclusive_group()
    fmt.add_argument('--json', action='store_true', help='Output JSON.')
    fmt.add_argument('--tsv',  action='store_true',
                     help='Output TSV: num<TAB>title<TAB>url<TAB>date<TAB>words')
    args = p.parse_args()

    html = load_html(args)
    chapters = extract(html)

    if args.json:
        json.dump(chapters, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write('\n')
    elif args.tsv:
        for c in chapters:
            sys.stdout.write(
                f"{c['num'] or ''}\t{c['title']}\t{c['url']}\t{c['date']}\t{c['words'] or ''}\n"
            )
    else:
        # Default: just URLs, one per line. Pipe-friendly.
        for c in chapters:
            sys.stdout.write(c['url'] + '\n')

    sys.stderr.write(f"Extracted {len(chapters)} chapters.\n")


if __name__ == '__main__':
    main()
