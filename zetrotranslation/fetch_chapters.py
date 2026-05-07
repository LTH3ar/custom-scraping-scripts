#!/usr/bin/env python3
"""
Fetch and clean chapter content from zetrotranslation.com (and similar
wp-manga themed translation sites).

The chapter prose lives in:
    <div class="reading-content">
        <div class="text-left"> ... <p>prose</p> ... </div>
    </div>

Everything else inside `reading-content` is translator chrome (the
"please rate this novel" warning, Ko-fi widget, share toolbar) and is
stripped before conversion.

Usage:
    # Single URL → stdout
    python fetch_chapters.py --url https://zetrotranslation.com/novel/<slug>/126/

    # Single URL → file
    python fetch_chapters.py --url ... -o ch126.md

    # Many URLs from a file (e.g. extract_urls.py output)
    python fetch_chapters.py urls.txt --out-dir chapters/

    # Piped from extract_urls.py
    python extract_urls.py --url <toc> | python fetch_chapters.py - --out-dir chapters/

    # Local HTML file (offline testing)
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
    requests = None  # only needed when actually fetching

UA = "Mozilla/5.0 (chapter-scraper; respectful)"

# Class / id markers for elements that wrap translator chrome rather than prose
FLUFF_CLASSES = ("chapter-warning", "btn-container", "kofi", "kofitext")
FLUFF_IDS = ("text-chapter-toolbar",)

# In-content title pattern: e.g., "Chapter 126: Special Short Story…"
CHAPTER_HEADING_RE = re.compile(
    r"^\s*Chapter\s+[\d\-A-Za-z]+\s*[:\-–—]\s*(.+?)\s*$"
)


# ---------------------------------------------------------------------------
# HTML → Markdown conversion
# ---------------------------------------------------------------------------

def is_fluff(tag):
    """True if a tag is translator chrome (warning, kofi widget, toolbar)."""
    if not isinstance(tag, Tag):
        return False
    if tag.get("id") in FLUFF_IDS:
        return True
    classes = tag.get("class") or []
    return any(any(f in c.lower() for f in FLUFF_CLASSES) for c in classes)


def node_to_md(node):
    """Recursively convert a BS4 node to markdown."""
    if isinstance(node, NavigableString):
        return re.sub(r"\s+", " ", str(node))

    if not isinstance(node, Tag):
        return ""

    name = node.name.lower()

    if name in ("script", "style", "noscript", "input", "link"):
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

    if name == "img":
        src = (node.get("src") or "").strip()
        alt = (node.get("alt") or "").strip()
        return f"![{alt}]({src})\n\n" if src else alt

    if name == "a":
        # Drop the link wrapper, keep the inner text. Translator-inserted
        # links inside chapter prose are usually self-promo we don't want.
        return "".join(node_to_md(c) for c in node.children)

    if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
        level = int(name[1])
        inner = "".join(node_to_md(c) for c in node.children).strip()
        return f"\n\n{'#' * level} {inner}\n\n" if inner else ""

    if name == "p":
        inner = "".join(node_to_md(c) for c in node.children).strip()
        return f"\n\n{inner}\n\n" if inner else ""

    if name == "span":
        # Most spans on this site are <span style="font-weight:400">, an
        # artifact of pasting from a word processor. Strip the wrapper, keep
        # the text. (No semantic value either way.)
        return "".join(node_to_md(c) for c in node.children)

    # default: descend
    return "".join(node_to_md(c) for c in node.children)


def tidy_markdown(md):
    md = html.unescape(md)
    md = re.sub(r"[ \t]+\n", "\n", md)            # trailing spaces on lines
    md = re.sub(r"\n{3,}", "\n\n", md)            # collapse blank-line runs
    return md.strip() + "\n"


# ---------------------------------------------------------------------------
# Page parsing
# ---------------------------------------------------------------------------

def extract_chapter(html_text, fmt="md"):
    """Return {'title', 'number', 'heading', 'body'} from a chapter page."""
    soup = BeautifulSoup(html_text, "html.parser")

    # --- chapter number / heading ---
    h1 = soup.find("h1", id="chapter-heading")
    full_heading = h1.get_text(" ", strip=True) if h1 else ""

    chap_input = soup.find("input", id="wp-manga-current-chap")
    chap_number = (chap_input.get("value", "").strip()
                   if chap_input else "")
    if not chap_number and full_heading:
        # Fall back to the trailing number(s) of the h1: "... - 126"
        m = re.search(r"-\s*([\d\-A-Za-z]+?)\s*$", full_heading)
        if m:
            chap_number = m.group(1)

    # --- locate the prose container ---
    content = soup.select_one("div.reading-content div.text-left")
    if content is None:
        # Fallback: take div.reading-content directly. The fluff-stripping
        # in node_to_md will skip the warning / toolbar even from here.
        content = soup.find("div", class_="reading-content")
        if content is None:
            raise ValueError("No reading-content div found.")

    # --- pull the descriptive title from the first paragraph if present ---
    descriptive_title = ""
    for p in content.find_all("p", limit=4):
        txt = p.get_text(" ", strip=True)
        if not txt:
            continue
        m = CHAPTER_HEADING_RE.match(txt)
        if m:
            descriptive_title = m.group(1).strip()
            p.decompose()  # don't render the title twice
            break
        # If the first non-empty paragraph isn't a chapter heading, stop —
        # we don't want to delete a stray match deep in the body.
        break

    # --- render ---
    if fmt == "html":
        for tag in content.find_all(True):
            tag.attrs.pop("style", None)
        for tag in list(content.find_all(True)):
            if is_fluff(tag) or tag.name in ("script", "style", "input"):
                tag.decompose()
        body = content.decode_contents()
    elif fmt == "txt":
        for tag in list(content.find_all(True)):
            if is_fluff(tag) or tag.name in ("script", "style", "input"):
                tag.decompose()
        body = re.sub(r"\n{3,}", "\n\n",
                      content.get_text("\n", strip=True))
    else:  # md
        body = tidy_markdown(node_to_md(content))

    return {
        "title": descriptive_title or full_heading,
        "number": chap_number,
        "heading": full_heading,
        "body": body,
    }


# ---------------------------------------------------------------------------
# Filename helper
# ---------------------------------------------------------------------------

def filename_for(url, title="", fmt="md"):
    """Sortable filename. URL slug is the chapter id like '126', '124-end',
    '122-123' — keep it as-is but zero-pad the leading number to 3 digits."""
    slug = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    ext = {"md": "md", "txt": "txt", "html": "html"}[fmt]
    m = re.match(r"^(\d+)(.*)$", slug)
    if m:
        num = int(m.group(1))
        return f"chapter-{num:03d}{m.group(2)}.{ext}"
    return f"chapter-{slug or 'unknown'}.{ext}"


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
            print(f"Wrote {args.output} (chap {ch['number']}: {ch['title'][:50]})", file=sys.stderr)
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
            print(f"Wrote {args.output} (chap {ch['number']}: {ch['title'][:50]})", file=sys.stderr)
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
        path = os.path.join(out_dir, filename_for(url, fmt=args.format))

        if not args.overwrite and os.path.exists(path):
            print(f"[{i:>3}/{len(urls)}] skip (exists): {os.path.basename(path)}", file=sys.stderr)
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
        print(f"[{i:>3}/{len(urls)}] chap {ch['number']}  →  {os.path.basename(path)}", file=sys.stderr)

        if args.combined:
            combined_chunks.append(text)
        if i < len(urls):
            time.sleep(args.delay)

    if args.combined and combined_chunks:
        sep = "\n\n---\n\n" if args.format == "md" else "\n\n" + "=" * 60 + "\n\n"
        with open(args.combined, "w", encoding="utf-8") as g:
            g.write(sep.join(combined_chunks))
        print(f"Combined → {args.combined}", file=sys.stderr)


def format_output(ch, fmt):
    """Wrap a parsed chapter into the final document text."""
    if fmt == "md":
        # Build a friendly heading: "Chapter <num>: <title>" if both present,
        # otherwise whichever we have.
        if ch["number"] and ch["title"] and ch["title"] != ch["heading"]:
            heading = f"Chapter {ch['number']}: {ch['title']}"
        elif ch["title"]:
            heading = ch["title"]
        elif ch["number"]:
            heading = f"Chapter {ch['number']}"
        else:
            heading = ""
        return f"# {heading}\n\n{ch['body']}" if heading else ch["body"]
    return ch["body"]


if __name__ == "__main__":
    main()
