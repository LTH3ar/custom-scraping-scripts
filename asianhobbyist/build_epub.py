#!/usr/bin/env python3
"""
Build an EPUB from a directory of chapter Markdown files.

Expected input: chapters/chapter-001-*.md, chapter-002-*.md, ...
(the format produced by fetch_chapters.py).

Usage:
    python build_epub.py chapters/ --out novel.epub \
        --title "The World's Strongest Knight..." \
        --author "KAZU" \
        --cover cover.jpg \
        --language en \
        --identifier "curspe-strongest-knight"

The first H1 in each MD file becomes the chapter title in the EPUB TOC.
Blockquote-style boxes (Forum Thread / System Notification / Comments) are
preserved as styled `<blockquote>` elements with a coloured left border.
"""

import argparse
import os
import re
import sys
from pathlib import Path

try:
    from ebooklib import epub
except ImportError:
    sys.exit("Need ebooklib. Run: pip install ebooklib")
try:
    import markdown as md_lib
except ImportError:
    sys.exit("Need markdown. Run: pip install markdown")


CSS = """
body { font-family: Georgia, "Times New Roman", serif; line-height: 1.6; }
h1   { font-size: 1.6em; margin: 1em 0 0.8em; text-align: center; }
h2   { font-size: 1.3em; margin-top: 1.5em; }
p    { text-indent: 1.2em; margin: 0.4em 0; }
em   { font-style: italic; }
strong { font-weight: bold; }

/* The forum/system/comment boxes survive as blockquotes from the markdown.
   Different first-line markers (📰, ⚙, 💬) tell readers them apart, but we
   also give the whole quote a subtle frame so it visually separates from
   the prose. */
blockquote {
  border-left: 3px solid #888;
  background: #f4f4f4;
  margin: 1em 0;
  padding: 0.6em 1em;
  font-size: 0.95em;
}
blockquote p { text-indent: 0; }

hr { border: none; border-top: 1px solid #ccc; margin: 2em 0; }

/* Cover page: scale the cover image to fit the screen rather than render at
   the image's native pixel size (which can overflow on small e-readers and
   look tiny on large ones). */
body.cover { margin: 0; padding: 0; text-align: center; }
img.cover-img, .cover img { max-width: 100%; max-height: 100vh; height: auto; }
"""

CHAP_NUM_RE = re.compile(r"chapter-(\d+)", re.IGNORECASE)


def parse_chapter_file(path: Path):
    """Read a chapter MD file. Returns (number, title, body_html)."""
    text = path.read_text(encoding="utf-8")
    # First H1 is the chapter title; everything after is the body
    m = re.match(r"\s*#\s+(.+?)\n", text)
    if m:
        title = m.group(1).strip()
        body_md = text[m.end():].lstrip()
    else:
        title = path.stem
        body_md = text

    # Convert body markdown to HTML. The chapter title becomes the EPUB
    # section header, so we don't repeat it inside the body.
    body_html = md_lib.markdown(body_md, extensions=["extra"])

    num_match = CHAP_NUM_RE.search(path.name)
    num = int(num_match.group(1)) if num_match else 0
    return num, title, body_html


def collect_chapters(chapters_dir: Path):
    """Yield (number, title, html, source_path) sorted by chapter number."""
    files = sorted(chapters_dir.glob("chapter-*.md"))
    if not files:
        sys.exit(f"No chapter-*.md files found in {chapters_dir}")
    parsed = [(*parse_chapter_file(f), f) for f in files]
    parsed.sort(key=lambda t: (t[0], t[3].name))  # by number, then filename
    return parsed


def build_epub(
    chapters_dir: Path,
    out_path: Path,
    title: str,
    author: str = "Unknown",
    language: str = "en",
    identifier: str | None = None,
    cover_path: Path | None = None,
    publisher: str | None = None,
    description: str | None = None,
):
    book = epub.EpubBook()
    book.set_identifier(identifier or f"book-{title.lower().replace(' ', '-')[:40]}")
    book.set_title(title)
    book.set_language(language)
    book.add_author(author)
    if publisher:
        book.add_metadata("DC", "publisher", publisher)
    if description:
        book.add_metadata("DC", "description", description)

    cover_item = None
    if cover_path and cover_path.exists():
        with open(cover_path, "rb") as f:
            cover_bytes = f.read()
        ext = cover_path.suffix.lower().lstrip(".") or "jpg"
        media_type = {"jpg": "image/jpeg", "jpeg": "image/jpeg",
                      "png": "image/png", "gif": "image/gif",
                      "webp": "image/webp"}.get(ext, "image/jpeg")
        cover_filename = f"cover.{ext}"

        # Cover image, marked with the EPUB3 `cover-image` property so e-readers
        # use it as the library thumbnail.
        cover_image = epub.EpubItem(
            uid="cover-img", file_name=cover_filename,
            media_type=media_type, content=cover_bytes,
        )
        # ebooklib looks at this attribute when emitting properties="cover-image"
        cover_image.properties = ["cover-image"]
        book.add_item(cover_image)
        # Also register as guide/cover-image metadata for old EPUB2 readers.
        book.add_metadata(None, "meta", "", {"name": "cover", "content": "cover-img"})

        # Cover page. Using a raw EpubItem (not EpubHtml) because EpubHtml
        # parses content through lxml and rebuilds the <head>, which strips
        # any inline <style>. With EpubItem the bytes are written verbatim.
        cover_xhtml = (
            "<?xml version='1.0' encoding='utf-8'?>\n"
            "<!DOCTYPE html>\n"
            f"<html xmlns='http://www.w3.org/1999/xhtml' lang='{language}'>"
            "<head><meta charset='utf-8'/><title>Cover</title>"
            "<style type='text/css'>"
            "body{margin:0;padding:0;text-align:center;}"
            "img{max-width:100%;max-height:100vh;height:auto;}"
            "</style></head>"
            f"<body><img src='{cover_filename}' alt='Cover'/></body>"
            "</html>"
        ).encode("utf-8")
        cover_item = epub.EpubItem(
            uid="cover", file_name="cover.xhtml",
            media_type="application/xhtml+xml", content=cover_xhtml,
        )
        book.add_item(cover_item)

    css_item = epub.EpubItem(
        uid="style", file_name="style/style.css",
        media_type="text/css", content=CSS,
    )
    book.add_item(css_item)

    # Spine = reading order. Cover first so it's the opening page when the
    # book is opened; nav second; chapters after.
    spine = []
    if cover_item is not None:
        spine.append(cover_item)
    spine.append("nav")

    toc_entries = []
    chapters = collect_chapters(chapters_dir)
    print(f"Adding {len(chapters)} chapters…", file=sys.stderr)

    for num, ch_title, body_html, src in chapters:
        file_name = f"chap_{num:03d}.xhtml"
        ch = epub.EpubHtml(
            title=ch_title,
            file_name=file_name,
            lang=language,
        )
        ch.content = (
            f"<html xmlns='http://www.w3.org/1999/xhtml'>"
            f"<head><title>{ch_title}</title>"
            f"<link rel='stylesheet' href='style/style.css' type='text/css'/>"
            f"</head><body><h1>{ch_title}</h1>{body_html}</body></html>"
        )
        ch.add_item(css_item)
        book.add_item(ch)
        spine.append(ch)
        toc_entries.append(epub.Link(file_name, ch_title, f"chap{num:03d}"))

    book.toc = tuple(toc_entries)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = spine

    out_path.parent.mkdir(parents=True, exist_ok=True)
    epub.write_epub(str(out_path), book)
    print(f"Wrote {out_path} ({out_path.stat().st_size:,} bytes)", file=sys.stderr)
    return out_path


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("chapters_dir", type=Path, help="Directory of chapter-*.md files.")
    p.add_argument("--out", "-o", type=Path, required=True, help="Output .epub path.")
    p.add_argument("--title", required=True)
    p.add_argument("--author", default="Unknown")
    p.add_argument("--language", default="en")
    p.add_argument("--identifier", help="Unique book ID (defaults to slug of title).")
    p.add_argument("--cover", type=Path, help="Cover image (.jpg/.png).")
    p.add_argument("--publisher")
    p.add_argument("--description")
    args = p.parse_args()

    build_epub(
        chapters_dir=args.chapters_dir,
        out_path=args.out,
        title=args.title,
        author=args.author,
        language=args.language,
        identifier=args.identifier,
        cover_path=args.cover,
        publisher=args.publisher,
        description=args.description,
    )


if __name__ == "__main__":
    main()
