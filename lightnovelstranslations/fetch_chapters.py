#!/usr/bin/env python3
"""
Fetch and clean chapter content from lightnovelstranslations.com.

The chapter prose lives in:
    <div class="text_story">
        <h2>Title</h2>
        <p>...</p>
        ...
    </div>

Cruft to strip:
  - Ad blocks: <div class="row"> wrapping <div class="adv_content_N ads_content
    ads-section ...">, plus any <ins class="adsbygoogle">, <script>, <iframe>.
  - Navigation: <div id="textbox"> (the [previous_page] / [next_page]
    template tokens that don't render).
  - Empty <p>&nbsp;</p> padding paragraphs around the navigation.

Filenames in batch mode use a position counter (chapter-001.md, chapter-002.md
…) rather than the URL slug, because the site's reading order is DOM order,
not numeric chapter order — see extract_urls.py.

Usage:
    # Single URL → stdout
    python fetch_chapters.py --url https://lightnovelstranslations.com/novel/<slug>/<chap>/

    # Single URL → file
    python fetch_chapters.py --url ... -o ch001.md

    # Many URLs from a file (output of extract_urls.py)
    python fetch_chapters.py urls.txt --out-dir chapters/

    # Piped from extract_urls.py
    python extract_urls.py --url <toc> | python fetch_chapters.py - --out-dir chapters/

    # Local HTML for testing
    python fetch_chapters.py --html-file sample.html

Options:
    --delay SECONDS      Sleep between requests (default 1.0).
    --combined PATH      Also write a single file with every chapter joined.
    --overwrite          Re-fetch even if the output file already exists.
    --format {md,txt,html}   Output format (default md).
"""

import argparse
import html
import os
import re
import sys
import time
from urllib.parse import urlparse

try:
    from bs4 import BeautifulSoup, NavigableString, Tag
except ImportError:
    sys.exit("This script needs BeautifulSoup. Run: pip install beautifulsoup4")

try:
    import requests
except ImportError:
    requests = None

UA = "Mozilla/5.0 (chapter-scraper; respectful)"

# Class / id markers for elements that wrap ads or navigation chrome
FLUFF_CLASS_PATTERNS = (
    "ads_content", "ads-section", "advert", "adsbygoogle",
    re.compile(r"\badv_content_\d+\b"),
    re.compile(r"\badv_below\b"),
)
FLUFF_IDS = ("textbox",)


def is_fluff(tag):
    """True if a tag is an ad block or navigation placeholder."""
    if not isinstance(tag, Tag):
        return False
    if tag.get("id") in FLUFF_IDS:
        return True
    classes = tag.get("class") or []
    for c in classes:
        cl = c.lower()
        for pat in FLUFF_CLASS_PATTERNS:
            if isinstance(pat, str):
                if pat in cl:
                    return True
            else:
                if pat.search(cl):
                    return True
    return False


# ---------------------------------------------------------------------------
# HTML → Markdown conversion
# ---------------------------------------------------------------------------

def node_to_md(node):
    if isinstance(node, NavigableString):
        return re.sub(r"\s+", " ", str(node))

    if not isinstance(node, Tag):
        return ""

    name = node.name.lower()

    if name in ("script", "style", "noscript", "ins", "iframe", "input", "link"):
        return ""

    if is_fluff(node):
        return ""

    if name in ("b", "strong"):
        inner = "".join(node_to_md(c) for c in node.children).strip()
        return f"**{inner}**" if inner else ""

    if name in ("i", "em"):
        inner = "".join(node_to_md(c) for c in node.children).strip()
        return f"*{inner}*" if inner else ""

    if name == "br":
        return "  \n"

    if name == "hr":
        return "\n\n---\n\n"

    if name == "img":
        src = (node.get("src") or "").strip()
        alt = (node.get("alt") or "").strip()
        # Skip tiny pixel-counter / tracking images
        if src and not any(s in src.lower() for s in ("pixel", "1x1", "blank")):
            return f"![{alt}]({src})\n\n"
        return ""

    if name == "a":
        # Translator-inserted links inside chapter prose are usually
        # navigation / self-promo. Drop the link wrapper, keep the text.
        return "".join(node_to_md(c) for c in node.children)

    if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
        level = int(name[1])
        inner = "".join(node_to_md(c) for c in node.children).strip()
        return f"\n\n{'#' * level} {inner}\n\n" if inner else ""

    if name == "p":
        inner = "".join(node_to_md(c) for c in node.children).strip()
        return f"\n\n{inner}\n\n" if inner else ""

    if name == "span":
        return "".join(node_to_md(c) for c in node.children)

    return "".join(node_to_md(c) for c in node.children)


def tidy_markdown(md):
    md = html.unescape(md)
    md = md.replace("\xa0", " ")                  # &nbsp;
    md = re.sub(r"[ \t]+\n", "\n", md)
    md = re.sub(r"\n{3,}", "\n\n", md)
    # Collapse stray "---" runs (e.g., back-to-back hr lines around stripped nav)
    md = re.sub(r"(\n\n---\n\n){2,}", "\n\n---\n\n", md)
    md = re.sub(r"^\s*---\s*\n+", "", md)         # leading hr
    md = re.sub(r"\n+\s*---\s*$", "", md)         # trailing hr
    return md.strip() + "\n"


# ---------------------------------------------------------------------------
# Page parsing
# ---------------------------------------------------------------------------

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


def extract_chapter(html_text, fmt="md"):
    """Return {'title', 'label', 'body'} extracted from a chapter page."""
    soup = BeautifulSoup(html_text, "html.parser")

    content = soup.select_one("div.text_story")
    if content is None:
        raise ValueError("No <div class='text_story'> found on this page.")

    # Title from the inner <h2>. (The page also has a global <h1> but it's
    # the site logo, not the chapter title.)
    title_full = ""
    label = ""
    title_only = ""
    h2 = content.find("h2")
    if h2:
        title_full = re.sub(r"\s+", " ", h2.get_text(" ", strip=True))
        h2.decompose()  # don't render the title twice in the body
        m = LABEL_RE.match(title_full)
        if m:
            label = re.sub(r"\s+", " ", m.group(1)).strip()
            title_only = m.group(2).strip()
        else:
            title_only = title_full

    # Render body
    if fmt == "html":
        for tag in list(content.find_all(True)):
            if is_fluff(tag) or tag.name in ("script", "style", "ins", "iframe", "input"):
                tag.decompose()
        for tag in content.find_all(True):
            tag.attrs.pop("style", None)
        body = content.decode_contents()
    elif fmt == "txt":
        for tag in list(content.find_all(True)):
            if is_fluff(tag) or tag.name in ("script", "style", "ins", "iframe", "input"):
                tag.decompose()
        body = re.sub(r"\n{3,}", "\n\n",
                      content.get_text("\n", strip=True))
    else:  # md
        body = tidy_markdown(node_to_md(content))

    return {
        "title": title_full,        # "Prologue: Waking up, My House…"
        "label": label,             # "Prologue"
        "title_only": title_only,   # "Waking up, My House Was in a Different World"
        "body": body,
    }


# ---------------------------------------------------------------------------
# Filename helper
# ---------------------------------------------------------------------------

def filename_for(url, position=None, fmt="md"):
    """Build a filename for a chapter file.

    `position` is the chapter's index in the reading order (1-based). When
    given, we use it for the leading zero-padded number, ignoring the URL
    slug — because on this site reading order ≠ numeric chapter order
    (decimals interleave with main chapters, and split chapters sit
    between their parent and the next one).

    When position is missing (single-URL standalone use), fall back to a
    slug-based filename so the result is at least informative.
    """
    ext = {"md": "md", "txt": "txt", "html": "html"}[fmt]
    if position is not None:
        return f"chapter-{int(position):03d}.{ext}"
    slug = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1] or "unknown"
    return f"chapter-{slug}.{ext}"


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def fetch(url, session):
    r = session.get(url, headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    return r.text


def read_url_list(path):
    src = sys.stdin if path == "-" else open(path, "r", encoding="utf-8")
    with src:
        for line in src:
            line = line.strip()
            if line and not line.startswith("#"):
                yield line


def format_output(ch, fmt):
    """Wrap a parsed chapter into the final document text."""
    if fmt != "md":
        return ch["body"]
    heading = ch.get("title") or ""
    return f"# {heading}\n\n{ch['body']}" if heading else ch["body"]


def main():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = p.add_mutually_exclusive_group()
    src.add_argument("urls_file", nargs="?", help="File of URLs, one per line, or `-` for stdin.")
    src.add_argument("--url", help="Single chapter URL.")
    src.add_argument("--html-file", help="Local HTML file (for testing).")

    p.add_argument("-o", "--output", help="Output file (single-URL mode). Default: stdout.")
    p.add_argument("--out-dir", help="Output directory (batch mode). Default: ./chapters")
    p.add_argument("--combined", help="Also write all chapters concatenated into this file.")
    p.add_argument("--format", choices=("md", "txt", "html"), default="md")
    p.add_argument("--delay", type=float, default=1.0, help="Seconds between requests.")
    p.add_argument("--overwrite", action="store_true", help="Re-fetch even if output exists.")
    args = p.parse_args()

    # ---- single local-file mode -------------------------------------------
    if args.html_file:
        with open(args.html_file, "r", encoding="utf-8") as f:
            ch = extract_chapter(f.read(), args.format)
        out = format_output(ch, args.format)
        if args.output:
            with open(args.output, "w", encoding="utf-8") as g:
                g.write(out)
            print(f"Wrote {args.output} ({ch['title'][:60]})", file=sys.stderr)
        else:
            sys.stdout.write(out)
        return

    if requests is None:
        sys.exit("`requests` not installed. Run: pip install requests")
    session = requests.Session()

    # ---- single URL mode --------------------------------------------------
    if args.url:
        ch = extract_chapter(fetch(args.url, session), args.format)
        out = format_output(ch, args.format)
        if args.output:
            with open(args.output, "w", encoding="utf-8") as g:
                g.write(out)
            print(f"Wrote {args.output} ({ch['title'][:60]})", file=sys.stderr)
        else:
            sys.stdout.write(out)
        return

    # ---- batch mode -------------------------------------------------------
    if not args.urls_file:
        p.error("Provide a URLs file, --url, or --html-file.")

    out_dir = args.out_dir or "chapters"
    os.makedirs(out_dir, exist_ok=True)

    combined_chunks = []
    urls = list(read_url_list(args.urls_file))
    print(f"Fetching {len(urls)} chapters → {out_dir}/", file=sys.stderr)

    for i, url in enumerate(urls, 1):
        path = os.path.join(out_dir, filename_for(url, position=i, fmt=args.format))

        if not args.overwrite and os.path.exists(path):
            print(f"[{i:>3}/{len(urls)}] skip (exists): {os.path.basename(path)}",
                  file=sys.stderr)
            if args.combined:
                with open(path, "r", encoding="utf-8") as f:
                    combined_chunks.append(f.read())
            continue

        try:
            page_html = fetch(url, session)
            ch = extract_chapter(page_html, args.format)
        except Exception as e:
            print(f"[{i:>3}/{len(urls)}] FAIL {url}: {e}", file=sys.stderr)
            continue

        text = format_output(ch, args.format)
        with open(path, "w", encoding="utf-8") as g:
            g.write(text)
        print(f"[{i:>3}/{len(urls)}] {ch['label'] or 'Chapter'}  →  {os.path.basename(path)}",
              file=sys.stderr)

        if args.combined:
            combined_chunks.append(text)
        if i < len(urls):
            time.sleep(args.delay)

    if args.combined and combined_chunks:
        sep = "\n\n---\n\n" if args.format == "md" else "\n\n" + "=" * 60 + "\n\n"
        with open(args.combined, "w", encoding="utf-8") as g:
            g.write(sep.join(combined_chunks))
        print(f"Combined → {args.combined}", file=sys.stderr)


if __name__ == "__main__":
    main()
