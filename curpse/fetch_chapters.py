#!/usr/bin/env python3
"""
Fetch and clean chapter content from curspe.com novel pages.

The target on each page is `<div class="chapter-content ...">`. Output is
Markdown with the inline style="..." noise stripped, italics/bold preserved,
and the styled "Forum Thread" / "System Notification" / commenter boxes
turned into labelled blockquotes so they survive the conversion.

Usage:
    # 1) Single URL → stdout
    python fetch_chapters.py --url https://curspe.com/.../chapter-1-new-route/

    # 2) Single URL → file
    python fetch_chapters.py --url https://curspe.com/.../chapter-1-new-route/ -o ch01.md

    # 3) Many URLs from a file (one per line — e.g. output of extract_urls.py)
    python fetch_chapters.py urls.txt --out-dir chapters/

    # 4) From stdin
    python extract_urls.py page.html | python fetch_chapters.py - --out-dir chapters/

    # 5) Local HTML file (for testing without network)
    python fetch_chapters.py --html-file sample_chapter.html

Options:
    --delay SECONDS      Sleep between requests (default 1.0). Be polite.
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

# ---------------------------------------------------------------------------
# Box detection: the page wraps forum threads / system notifications / comment
# bubbles in <div style="..."> with distinctive colours. We detect by colour
# fingerprint rather than class (there are no useful classes).
# ---------------------------------------------------------------------------

def classify_box(div):
    """Return a label string for special styled boxes, or None if it's just a wrapper.

    Detection is by inline-style colour fingerprint only. Don't use text content
    here — a chapter that *contains* a System Notification would otherwise have
    its entire body classified as one. The site uses hex (#ff6b00) on the live
    page but some serializations produce rgb(255, 107, 0); we accept both.
    """
    style = (div.get("style") or "").lower()
    if not style:
        return None
    # Outer box has a real `border:` (or `border-left:` for the green comments).
    has_outer_border = ("border:" in style) or ("border-left:" in style)
    if not has_outer_border:
        return None

    def has(*needles):
        return any(n in style for n in needles)

    # System notification: deep blue
    if has("#00219b", "rgb(0, 33, 155)"):
        return "SYSTEM"
    # Forum thread: orange
    if has("#ff6b00", "rgb(255, 107, 0)"):
        return "FORUM"
    # Comment bubbles: green or blue
    if has("#00ff88", "rgb(0, 255, 136)", "#0066ff", "rgb(0, 102, 255)"):
        return "COMMENT"
    return None


# ---------------------------------------------------------------------------
# HTML → Markdown conversion (small, purpose-built)
# ---------------------------------------------------------------------------

def node_to_md(node, depth=0, in_box=False):
    """Recursively convert a BS4 node to a markdown string."""
    if isinstance(node, NavigableString):
        # collapse runs of whitespace but keep single spaces
        return re.sub(r"\s+", " ", str(node))

    if not isinstance(node, Tag):
        return ""

    name = node.name.lower()

    if name in ("script", "style", "noscript"):
        return ""

    if name in ("b", "strong"):
        inner = "".join(node_to_md(c, depth, in_box) for c in node.children).strip()
        return f"**{inner}**" if inner else ""

    if name in ("i", "em"):
        inner = "".join(node_to_md(c, depth, in_box) for c in node.children).strip()
        return f"*{inner}*" if inner else ""

    if name == "br":
        return "  \n"

    if name == "img":
        # Most images on these chapters are emoji SVGs — keep the alt only.
        alt = node.get("alt", "").strip()
        return alt

    if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
        level = int(name[1])
        inner = "".join(node_to_md(c, depth, in_box) for c in node.children).strip()
        return f"\n\n{'#' * level} {inner}\n\n"

    if name == "p":
        inner = "".join(node_to_md(c, depth, in_box) for c in node.children).strip()
        return f"\n\n{inner}\n\n" if inner else ""

    if name == "div":
        # Once we're inside a classified box, treat nested divs as plain
        # wrappers — otherwise the box's own header/body divs get re-wrapped.
        label = None if in_box else classify_box(node)
        if label:
            inner = "".join(node_to_md(c, depth + 1, in_box=True) for c in node.children).strip()
            quoted = "\n".join(f"> {line}" if line.strip() else ">"
                               for line in inner.splitlines())
            tag = {
                "SYSTEM":  "⚙ **System Notification**",
                "FORUM":   "📰 **Forum Thread**",
                "COMMENT": "💬",
            }[label]
            return f"\n\n> {tag}\n>\n{quoted}\n\n"
        # plain wrapper div — just descend
        return "".join(node_to_md(c, depth, in_box) for c in node.children)

    # default: descend
    return "".join(node_to_md(c, depth, in_box) for c in node.children)


def tidy_markdown(md):
    md = html.unescape(md)
    md = re.sub(r"[ \t]+\n", "\n", md)            # trailing spaces
    md = re.sub(r"\n{3,}", "\n\n", md)            # collapse blank lines
    md = re.sub(r"^\s+|\s+$", "", md)             # outer trim
    return md + "\n"


# ---------------------------------------------------------------------------
# Page parsing
# ---------------------------------------------------------------------------

def extract_chapter(html_text, fmt="md"):
    """Return {'title': str, 'body': str} extracted from a chapter page."""
    soup = BeautifulSoup(html_text, "html.parser")

    content = soup.find("div", class_=re.compile(r"\bchapter-content\b"))
    if content is None:
        raise ValueError("No <div class='chapter-content'> found on this page.")

    # Title comes from the first h1 *inside* chapter-content. A global selector
    # would pick up the site header ("Curspe") instead.
    title = ""
    first_h1 = content.find("h1")
    if first_h1:
        title = first_h1.get_text(strip=True)
        # Drop it from the body so we don't render the title twice
        first_h1.decompose()

    # Fallback to the page-level header if the content has no h1 of its own
    if not title:
        outer = soup.find("h1", class_=re.compile(r"\btext-primary\b"))
        if outer:
            title = outer.get_text(strip=True)

    if fmt == "html":
        for tag in content.find_all(True):
            tag.attrs.pop("style", None)
        body = content.decode_contents()
    elif fmt == "txt":
        body = content.get_text("\n", strip=True)
        body = re.sub(r"\n{3,}", "\n\n", body)
    else:  # md
        body = tidy_markdown(node_to_md(content))

    return {"title": title, "body": body}


# ---------------------------------------------------------------------------
# Filename helpers
# ---------------------------------------------------------------------------

CHAP_NUM_RE = re.compile(r"chapter-(\d+)", re.IGNORECASE)
CHAP_NUM_TITLE_RE = re.compile(r"Chapter\s+(\d+)", re.IGNORECASE)

def filename_for(url, title, fmt):
    """Stable, sortable filename like `chapter-001-new-route.md`."""
    slug = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    m = CHAP_NUM_RE.search(slug) or CHAP_NUM_TITLE_RE.search(title)
    num = int(m.group(1)) if m else 0
    rest = CHAP_NUM_RE.sub("", slug, count=1).strip("-") if m else slug
    ext = {"md": "md", "txt": "txt", "html": "html"}[fmt]
    return f"chapter-{num:03d}-{rest}.{ext}" if rest else f"chapter-{num:03d}.{ext}"


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def fetch(url, session):
    r = session.get(url, headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    return r.text


def read_url_list(path):
    if path == "-":
        src = sys.stdin
    else:
        src = open(path, "r", encoding="utf-8")
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
        out = f"# {ch['title']}\n\n{ch['body']}" if args.format == "md" else ch["body"]
        if args.output:
            with open(args.output, "w", encoding="utf-8") as g:
                g.write(out)
            print(f"Wrote {args.output} ({ch['title']})", file=sys.stderr)
        else:
            sys.stdout.write(out)
        return

    if requests is None:
        sys.exit("`requests` not installed. Run: pip install requests")
    session = requests.Session()

    # ---- single URL mode --------------------------------------------------
    if args.url:
        ch = extract_chapter(fetch(args.url, session), args.format)
        out = f"# {ch['title']}\n\n{ch['body']}" if args.format == "md" else ch["body"]
        if args.output:
            with open(args.output, "w", encoding="utf-8") as g:
                g.write(out)
            print(f"Wrote {args.output} ({ch['title']})", file=sys.stderr)
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
        # Pre-compute filename from URL alone so --overwrite check works without fetching
        slug_fname = filename_for(url, "", args.format)
        path = os.path.join(out_dir, slug_fname)

        if not args.overwrite and os.path.exists(path):
            print(f"[{i:>3}/{len(urls)}] skip (exists): {slug_fname}", file=sys.stderr)
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

        # Use real title for nicer filename when we got it
        better = filename_for(url, ch["title"], args.format)
        path = os.path.join(out_dir, better)
        text = f"# {ch['title']}\n\n{ch['body']}" if args.format == "md" else ch["body"]
        with open(path, "w", encoding="utf-8") as g:
            g.write(text)
        print(f"[{i:>3}/{len(urls)}] {ch['title']}  →  {better}", file=sys.stderr)

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
