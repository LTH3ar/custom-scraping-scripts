# curspe-novel-to-epub

Scrape a novel from curspe.com and bundle it as an EPUB. Four small Python scripts that compose: extract chapter URLs, fetch each chapter as Markdown, build the EPUB.

## Quick start

```bash
pip install requests beautifulsoup4 ebooklib markdown

# Drop all four .py files in the same folder, then:
python make_book.py \
  --toc-url "https://curspe.com/novels/<novel-slug>/" \
  --workdir ./book \
  --epub novel.epub
```

That's it. ~116 chapters at the default 1-second delay takes about two minutes. Title and cover are auto-detected from the page.

## The four scripts

| Script | What it does | Standalone? |
|---|---|---|
| `extract_urls.py` | Parses the chapter-list HTML and prints all chapter URLs | Yes |
| `fetch_chapters.py` | Downloads each URL, extracts `div.chapter-content`, converts to clean Markdown | Yes |
| `build_epub.py` | Bundles a directory of `chapter-NNN-*.md` files into an EPUB | Yes |
| `make_book.py` | Runs all three in sequence with auto-detected metadata | Yes |

`make_book.py` imports the work-horse functions from the others, so all four files must sit in the same directory.

## Common workflows

**Resume after a failed run.** Just rerun the same command — chapters that were already downloaded are skipped.

```bash
python make_book.py --toc-url "..." --workdir ./book --epub novel.epub
```

**Force a re-fetch.** Pass `--overwrite`.

**Hand-edit chapters before binding** (typos, awkward translations). Edit the markdown files in `book/chapters/`, then rebuild the EPUB only:

```bash
python make_book.py --skip-extract --skip-fetch \
  --workdir ./book --epub novel.epub --title "..." --author "..."
```

**Use your own cover.** Pass `--cover path/to/your.jpg` to override auto-detection.

**Just get the URL list** (no download, no EPUB):

```bash
python extract_urls.py --url "https://curspe.com/novels/<slug>/" > urls.txt
```

**Just rebuild an EPUB from existing markdown:**

```bash
python build_epub.py book/chapters/ --out novel.epub \
  --title "My Title" --author "KAZU" --cover book/cover.jpg
```

## What the EPUB contains

- Cover image as the **first reading page** (not just the library thumbnail)
- Auto-generated table of contents (EPUB3 nav + legacy NCX for old readers)
- Each chapter as a separate XHTML section
- Light CSS that gives the in-story Forum / System Notification / comment boxes a subtle frame so they stay distinct from prose

## Things to know

The scraper assumes curspe.com's current theme — specifically `<div class="chapter-content">` for the chapter body, `<h1 class="wn-title">` for the novel title, and WordPress's standard `wp-post-image` class for the cover. If the theme changes, the corresponding selectors in `fetch_chapters.py` and `make_book.py` are the things to update.

The default 1-second delay between chapter fetches is meant to be polite. Bump it up with `--delay 3` if you'd rather be slower; don't take it below ~0.5s.

This only works for sites that render their chapter list server-side. Sites that load chapters via JavaScript will return empty pages — you'd need a headless browser like Playwright instead.

The author field is not on the curspe.com TOC pages, so it ends up as "Unknown" unless you pass `--author`. The afterword in chapter 1 of this particular novel mentions "KAZU".

## License

Personal use. Don't redistribute scraped content. Be respectful of authors and translators — buy the official release if there is one.
