#!/usr/bin/env python3
"""
Extract chapter URLs from an asianhobbyist.com series page.

This site is awkward in two ways:
  1. The chapter list is a partial translation — only some segments of the
     novel are done, so chapter numbers have gaps (e.g. 29-55, then 229,
     then 231-260, then 261.2-274).
  2. URLs use two different slug schemes:
        .../the-world-is-overflowing-with-monster-chapter-29/   (older)
        .../overflowing-231/                                    (newer)
     so you can't reconstruct a URL from a chapter number — you must read
     the real href off the page.

Because of (2) we key everything off the visible "Ch. NNN" label and carry
the real href alongside. Because of (1), the --from / --to range filter is
the main tool for grabbing just the span you want.

Usage:
    # Everything the site has translated:
    python extract_urls.py --url <series-url>

    # Just a numeric range (inclusive). Decimals allowed.
    python extract_urls.py --url <series-url> --from 231 --to 274

    # From a saved HTML file:
    python extract_urls.py page.html --from 231 --to 274

    # Full metadata:
    python extract_urls.py --url <series-url> --tsv
    python extract_urls.py --url <series-url> --json
"""

import argparse
import json
import re
import sys

try:
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("Need BeautifulSoup. Run: pip install beautifulsoup4")

# Matches the link text "Ch. 231", "Ch.231", "Chapter 261.2", and tolerates
# messy suffixes like "Ch. 260.-2" (a translator typo for 260). We capture
# the leading integer always, plus an optional CLEAN ".N" decimal; any other
# trailing junk (".-2", "(Part 2)", etc.) is ignored for numbering purposes.
LABEL_RE = re.compile(r"^\s*(?:Ch\.?|Chapter)\s*(\d+)(?:\.(\d+))?", re.I)
# Any 2-segment same-host path. The "Ch. N" label filter (LABEL_RE) does the
# real work of identifying chapter links, so this just rules out off-site and
# single-segment links and works for any asianhobbyist novel regardless of
# the (inconsistent) chapter slug scheme.
CHAPTER_HREF_RE = re.compile(r"^https?://[^/]*asianhobbyist\.com/[^/]+/[^/]+/?$", re.I)


def extract(html, series_path_re=CHAPTER_HREF_RE):
    """Return a list of chapter dicts sorted ascending by chapter number.

    Each: {num (float), label (str), url, text}.
    De-duplicated by chapter number (first href wins).
    """
    soup = BeautifulSoup(html, "html.parser")
    seen = {}
    order = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not series_path_re.search(href):
            continue
        text = a.get_text(" ", strip=True)
        m = LABEL_RE.match(text)
        if not m:
            continue  # skips "Read First Chapter" / "Read Latest" buttons
        intpart = m.group(1)
        decpart = m.group(2)  # None unless a clean ".N" decimal was present
        if decpart is not None:
            num = float(f"{intpart}.{decpart}")
            label = f"{intpart}.{decpart}"
        else:
            num = float(intpart)
            label = intpart
        if num in seen:
            continue
        seen[num] = {
            "num": num,
            "label": label,          # clean numeric label, e.g. "261.2"
            "url": href,
            "text": text,            # original messy text, e.g. "Ch. 260.-2"
        }
        order.append(num)

    chapters = [seen[n] for n in sorted(seen)]
    return chapters


def in_range(num, lo, hi):
    if lo is not None and num < lo:
        return False
    if hi is not None and num > hi:
        return False
    return True


def load_html(args):
    if args.url:
        try:
            import requests
        except ImportError:
            sys.exit("`requests` not installed. Run: pip install requests")
        r = requests.get(args.url,
                         headers={"User-Agent": "Mozilla/5.0 (chapter-extractor)"},
                         timeout=30)
        r.raise_for_status()
        return r.text
    if args.path == "-":
        return sys.stdin.read()
    with open(args.path, "r", encoding="utf-8") as f:
        return f.read()


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("path", nargs="?", default="-",
                   help="HTML file path, or `-` for stdin (default).")
    p.add_argument("--url", help="Fetch the series page directly from this URL.")
    p.add_argument("--from", dest="lo", type=float, default=None,
                   help="Lowest chapter number to include (inclusive).")
    p.add_argument("--to", dest="hi", type=float, default=None,
                   help="Highest chapter number to include (inclusive).")
    fmt = p.add_mutually_exclusive_group()
    fmt.add_argument("--json", action="store_true", help="Output JSON.")
    fmt.add_argument("--tsv", action="store_true",
                     help="Output TSV: num<TAB>label<TAB>url")
    args = p.parse_args()

    html = load_html(args)
    chapters = extract(html)
    selected = [c for c in chapters if in_range(c["num"], args.lo, args.hi)]

    if args.json:
        json.dump(selected, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    elif args.tsv:
        for c in selected:
            sys.stdout.write(f"{c['label']}\t{c['text']}\t{c['url']}\n")
    else:
        for c in selected:
            sys.stdout.write(c["url"] + "\n")

    # Diagnostics to stderr: total found, selected, and any gaps in the
    # selected range so the user can see what's actually translated.
    msg = f"Found {len(chapters)} chapters total"
    if args.lo is not None or args.hi is not None:
        lo = args.lo if args.lo is not None else chapters[0]["num"] if chapters else 0
        hi = args.hi if args.hi is not None else chapters[-1]["num"] if chapters else 0
        msg += f"; {len(selected)} in range [{lo:g}, {hi:g}]"
        # Report integer chapters missing inside the requested span
        # Report integer chapters missing inside the requested span.
        # An integer slot counts as present if any selected chapter has that
        # integer part (so 261.2 means 261 is NOT considered missing).
        present_ints = {int(c["num"]) for c in selected}
        missing = [n for n in range(int(lo), int(hi) + 1) if n not in present_ints]
        if missing:
            # collapse consecutive runs for readability
            runs = []
            s = e = missing[0]
            for n in missing[1:]:
                if n == e + 1:
                    e = n
                else:
                    runs.append((s, e)); s = e = n
            runs.append((s, e))
            pretty = ", ".join(f"{a}" if a == b else f"{a}-{b}" for a, b in runs)
            msg += f"; gaps (untranslated/absent): {pretty}"
    sys.stderr.write(msg + ".\n")


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        # Happens when piping into head/less and the reader closes early.
        try:
            sys.stdout.close()
        except Exception:
            pass