# custom-scraping-scripts

Personal Python scripts that turn web novels from various translation sites into EPUBs.

Each subfolder is a self-contained pipeline for one source site. They share the same four-script shape:

- `extract_urls.py` — scrape the chapter list, emit URLs
- `fetch_chapters.py` — download each chapter, strip site chrome, save as Markdown
- `build_epub.py` — bundle the Markdown chapters into an EPUB
- `make_book.py` — orchestrator: TOC URL in, EPUB out

## Sites currently supported

- `curspe/` — [curspe.com](https://curspe.com)
- `zetro/` — [zetrotranslation.com](https://zetrotranslation.com) (and similar wp-manga themed sites)
- `lightnovelstranslations` — [lightnovelstranslations.com](https://lightnovelstranslations.com)

More will be added as I run into other sites — this isn't limited to these two.

## Usage

```bash
pip install requests beautifulsoup4 ebooklib markdown

cd <site>/
python make_book.py \
  --toc-url "<novel-toc-url>" \
  --workdir ./book \
  --epub novel.epub
```

Each subfolder has its own README with site-specific notes (different selectors, AJAX quirks, where the cover lives, etc.).

## Adding a new site

Copy the closest existing folder as a starting point and update:

1. **`extract_urls.py`** — selectors for the chapter list. If the list is loaded via AJAX (like wp-manga sites), also update the fetcher to call the right endpoint.
2. **`fetch_chapters.py`** — selector for the content container, plus any site-specific "chrome" classes to strip (translator notes, share buttons, donation widgets, etc.).
3. **`make_book.py`** — selectors for title / cover / author / description on the TOC page.
4. **`build_epub.py`** is generic and rarely needs changes.

## Please don't redistribute

These scripts are for personal reading convenience — making local EPUBs from chapters that would otherwise be scattered across many web pages. **Do not redistribute the EPUBs you produce.** Translation work is unpaid; the people doing it deserve the page views, Ko-fi tips, and Patreon support that come from people actually reading on their sites. If a novel has an official English release, buy it.