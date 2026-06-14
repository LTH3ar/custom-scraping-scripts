#!/usr/bin/env python3
"""
Fetch and clean chapter content from asianhobbyist.com.

Page structure:
    <h1 class="entry-title">Series Title</h1>
    <h2 ...>Ch. 231</h2>
    <div class="reader-content ...">
        <div class="announcement-wrap panel">...</div>      <- chrome
        <header>...</header>                                 <- chrome
        <div class="flex ... navig">Ch. 29 Ch. 30 ...</div>  <- chrome (chapter picker)
        <div class="entry-content">
            <div class="code-block code-block-5"><script>ads</script></div>   <- ad
            <div class="markdown markdown-main-panel ...">   <- THE PROSE
                <p>231. VS Arrogance (Part 2)</p>            <- in-content title
                <p>Power overflows.</p>
                ...
            </div>
            <div class="code-block code-block-2">Click Donate...</div>  <- ad
            <div class="code-block code-block-3">Patreon...</div>       <- ad
        </div>
        <div class="flex ... navig">...</div>                <- chrome
    </div>

We target `div.markdown-main-panel` (cleanest), falling back to
`div.entry-content` then `div.reader-content`, and strip the ad/donation
`code-block*` divs, scripts, nav rows, and announcement panels.

Filenames use the chapter number from the <h2> (supports decimals like
261.2 → chapter-261.2.md).

Usage:
    # Single URL → stdout / file
    python fetch_chapters.py --url https://www.asianhobbyist.com/<book>/<chap>/
    python fetch_chapters.py --url ... -o ch231.md

    # Batch from a URL file (output of extract_urls.py)
    python fetch_chapters.py urls.txt --out-dir chapters/

    # Piped
    python extract_urls.py --url <series> --from 231 --to 274 \\
        | python fetch_chapters.py - --out-dir chapters/

    # Local HTML for testing
    python fetch_chapters.py --html-file sample.html

Options:
    --delay SECONDS      Sleep between requests (default 1.0).
    --combined PATH      Also write one joined file.
    --overwrite          Re-fetch even if the output exists.
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

# Wrapper classes / ids that are ads, donations, navigation, or social chrome
FLUFF_CLASS_PATTERNS = (
    re.compile(r"\bcode-block\b"),       # ad / donation slots
    re.compile(r"\bnavig\b"),            # chapter-picker rows
    re.compile(r"\bannouncement\b"),
    re.compile(r"\bselect-chapter\b"),
    re.compile(r"\bselect-pagination\b"),
    re.compile(r"\bw-pickchapter\b"),
    re.compile(r"\bvuukle\b"),
    re.compile(r"\bsharedaddy\b"),
    re.compile(r"\bsocial\b"),
)


def is_fluff(tag):
    if not isinstance(tag, Tag):
        return False
    classes = tag.get("class") or []
    for c in classes:
        cl = c.lower()
        for pat in FLUFF_CLASS_PATTERNS:
            if pat.search(cl):
                return True
    return False


# ---------------------------------------------------------------------------
# HTML → Markdown
# ---------------------------------------------------------------------------

def node_to_md(node):
    if isinstance(node, NavigableString):
        return re.sub(r"\s+", " ", str(node))
    if not isinstance(node, Tag):
        return ""

    name = node.name.lower()
    if name in ("script", "style", "noscript", "ins", "iframe", "input",
                "link", "select", "option", "label", "svg", "button", "header"):
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
        if src and not any(s in src.lower() for s in ("pixel", "1x1", "blank", "spacer")):
            return f"![{alt}]({src})\n\n"
        return ""
    if name == "a":
        # Drop link wrappers, keep text (chapter prose rarely has real links;
        # the ones present are nav / donation which we want gone anyway).
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
    md = md.replace("\xa0", " ")
    md = re.sub(r"[ \t]+\n", "\n", md)
    md = re.sub(r"\n{3,}", "\n\n", md)
    md = re.sub(r"(\n\n---\n\n){2,}", "\n\n---\n\n", md)
    md = re.sub(r"^\s*---\s*\n+", "", md)
    md = re.sub(r"\n+\s*---\s*$", "", md)
    return md.strip() + "\n"


# ---------------------------------------------------------------------------
# Page parsing
# ---------------------------------------------------------------------------

CHNUM_RE = re.compile(r"(?:Ch\.?|Chapter)\s*([\d]+(?:\.[\d]+)?)", re.I)
# In-content title line like "231. VS Arrogance (Part 2)"
INCONTENT_TITLE_RE = re.compile(r"^\s*([\d]+(?:\.[\d]+)?)\s*[.．:]\s*(.+?)\s*$")


def extract_chapter(html_text, fmt="md"):
    """Return {'number', 'series_title', 'title', 'body'}."""
    soup = BeautifulSoup(html_text, "html.parser")

    # Series title from the page-level h1.entry-title
    series_title = ""
    h1 = soup.select_one("h1.entry-title")
    if h1:
        series_title = re.sub(r"\s+", " ", h1.get_text(" ", strip=True))

    # Chapter number from the h2 that reads "Ch. NNN"
    number = ""
    for h2 in soup.find_all("h2"):
        m = CHNUM_RE.search(h2.get_text(" ", strip=True))
        if m:
            number = m.group(1)
            break

    # Locate the prose container, cleanest first.
    content = (soup.select_one("div.reader-content div.markdown-main-panel")
               or soup.select_one("div.reader-content div.entry-content")
               or soup.select_one("div.reader-content")
               or soup.select_one("div.entry-content"))
    if content is None:
        raise ValueError("No reader-content / entry-content / markdown panel found.")

    # Remove fluff subtrees up front so they can't leak into any format path.
    for tag in list(content.find_all(True)):
        if is_fluff(tag) or tag.name in ("script", "style", "ins", "iframe",
                                         "select", "option", "label", "svg",
                                         "button", "header", "nav"):
            tag.decompose()

    # Detect the in-content title. On live pages it's an <h3> like
    # "231. VS Arrogance (Part 2)"; in some chapters it's the first <p>.
    # Look at the first non-empty heading/paragraph and lift it out so we
    # can use it as the chapter title and avoid printing it twice.
    title = ""
    for el in content.find_all(["h1", "h2", "h3", "h4", "p"], limit=4):
        txt = el.get_text(" ", strip=True)
        if not txt:
            continue
        m = INCONTENT_TITLE_RE.match(txt)
        if m and (not number
                  or m.group(1) == number
                  or m.group(1).split(".")[0] == str(number).split(".")[0]):
            title = m.group(2).strip()
            el.decompose()
        break  # only consider the first non-empty block

    # Render
    if fmt == "html":
        for tag in content.find_all(True):
            tag.attrs.pop("style", None)
        body = content.decode_contents()
    elif fmt == "txt":
        body = re.sub(r"\n{3,}", "\n\n", content.get_text("\n", strip=True))
    else:
        body = tidy_markdown(node_to_md(content))

    return {
        "number": number,
        "series_title": series_title,
        "title": title,
        "body": body,
    }


# ---------------------------------------------------------------------------
# Filename helper
# ---------------------------------------------------------------------------

def filename_for(url, number=None, fmt="md"):
    """Filename based on chapter number when known (handles decimals:
    261.2 → chapter-261.2.md). Falls back to URL slug otherwise."""
    ext = {"md": "md", "txt": "txt", "html": "html"}[fmt]
    if number:
        # zero-pad the integer part to 3 digits, keep any decimal
        if "." in str(number):
            intpart, dec = str(number).split(".", 1)
            return f"chapter-{int(intpart):03d}.{dec}.{ext}"
        return f"chapter-{int(number):03d}.{ext}"
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
    if fmt != "md":
        return ch["body"]
    if ch["number"] and ch["title"]:
        heading = f"Chapter {ch['number']}: {ch['title']}"
    elif ch["number"]:
        heading = f"Chapter {ch['number']}"
    elif ch["title"]:
        heading = ch["title"]
    else:
        heading = ""
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

    if args.html_file:
        with open(args.html_file, "r", encoding="utf-8") as f:
            ch = extract_chapter(f.read(), args.format)
        out = format_output(ch, args.format)
        if args.output:
            with open(args.output, "w", encoding="utf-8") as g:
                g.write(out)
            print(f"Wrote {args.output} (Ch. {ch['number']}: {ch['title'][:50]})", file=sys.stderr)
        else:
            sys.stdout.write(out)
        return

    if requests is None:
        sys.exit("`requests` not installed. Run: pip install requests")
    session = requests.Session()

    if args.url:
        ch = extract_chapter(fetch(args.url, session), args.format)
        out = format_output(ch, args.format)
        if args.output:
            with open(args.output, "w", encoding="utf-8") as g:
                g.write(out)
            print(f"Wrote {args.output} (Ch. {ch['number']}: {ch['title'][:50]})", file=sys.stderr)
        else:
            sys.stdout.write(out)
        return

    if not args.urls_file:
        p.error("Provide a URLs file, --url, or --html-file.")

    out_dir = args.out_dir or "chapters"
    os.makedirs(out_dir, exist_ok=True)

    combined_chunks = []
    urls = list(read_url_list(args.urls_file))
    print(f"Fetching {len(urls)} chapters → {out_dir}/", file=sys.stderr)

    for i, url in enumerate(urls, 1):
        # We don't know the chapter number until we fetch, so for the skip
        # check we first try the slug-based name; once fetched we rename to
        # the number-based name. Simpler: fetch, then write number-based.
        try:
            page_html = fetch(url, session)
            ch = extract_chapter(page_html, args.format)
        except Exception as e:
            print(f"[{i:>3}/{len(urls)}] FAIL {url}: {e}", file=sys.stderr)
            continue

        path = os.path.join(out_dir, filename_for(url, number=ch["number"], fmt=args.format))
        if not args.overwrite and os.path.exists(path):
            print(f"[{i:>3}/{len(urls)}] skip (exists): {os.path.basename(path)}", file=sys.stderr)
            if args.combined:
                with open(path, "r", encoding="utf-8") as f:
                    combined_chunks.append(f.read())
            # still respect delay politeness even on skip-after-fetch? No—
            # we already fetched, so just continue.
            continue

        text = format_output(ch, args.format)
        with open(path, "w", encoding="utf-8") as g:
            g.write(text)
        print(f"[{i:>3}/{len(urls)}] Ch. {ch['number']}  →  {os.path.basename(path)}", file=sys.stderr)

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
    try:
        main()
    except BrokenPipeError:
        try:
            sys.stdout.close()
        except Exception:
            pass
