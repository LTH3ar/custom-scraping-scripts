# zetro-novel-to-epub

Scrape a novel from zetrotranslation.com (or any wp-manga themed translation site) and bundle it as an EPUB. Same four-script architecture as the curspe pipeline, but adapted for the wp-manga theme.

## Quick start

```bash
pip install requests beautifulsoup4 ebooklib markdown

# Drop all four .py files in the same folder, then:
python make_book.py \
  --toc-url "https://zetrotranslation.com/novel/<novel-slug>/" \
  --workdir ./book \
  --epub novel.epub
```

That's it. ~90 chapters at the default 1-second delay finishes in about two minutes.

## The four scripts

| Script | What it does | Standalone? |
|---|---|---|
| `extract_urls.py` | Hits the wp-manga AJAX endpoint, parses the chapter list | Yes |
| `fetch_chapters.py` | Downloads each chapter, strips translator chrome, → Markdown | Yes |
| `build_epub.py` | Bundles a directory of `chapter-NNN.md` files into an EPUB | Yes |
| `make_book.py` | Runs all three with auto-detected title / cover / author / description | Yes |

`make_book.py` imports the work-horse functions from the other three, so all four files must sit in the same directory.

## What's different from the curspe pipeline

**The chapter list is loaded via AJAX.** A plain GET on the novel page returns an empty list — wp-manga populates it via JavaScript at runtime. `extract_urls.py` handles this by scraping the manga ID (`data-id="…"`) from the page and POSTing to `/wp-admin/admin-ajax.php` with `action=manga_get_chapters`. This same trick works on most sites built on the wp-manga theme.

**Three chapter title shapes.** Single chapters (`126 - Title`), ranges (`122-123 - Title1 || Title2`), and suffix tags (`124 END - …`). The parser handles all three; combined `||` titles are joined with ` / ` for nicer display.

**Translator chrome filtering.** Each chapter page has a `chapter-warning` block ("Please rate 5★", Ko-fi widget), a hidden input, and a share toolbar. All filtered by class/id, not by text content, so it's robust if the translator changes the warning message.

**Cleaner metadata.** wp-manga gives us `og:image`, `og:description`, `.post-title`, and `.author-content` as semantic hooks. The author auto-detect picks up the original Japanese name (e.g., 水島紗鳥) — pass `--author "Sato Mizushima"` if you want a romanized version.

**Locked chapters.** The site supports premium / paid chapters via `<span class="coin">Premium</span>` and `premium-chap` classes. Locked chapters are skipped from the URL list by default; pass `--include-locked` to `extract_urls.py` to include them.

## Common workflows

**Resume after a failed run.** Just rerun the same command — chapters that were already downloaded are skipped.

```bash
python make_book.py --toc-url "..." --workdir ./book --epub novel.epub
```

**Force a re-fetch.** Pass `--overwrite`.

**Hand-edit chapters before binding.** Edit the markdown files in `book/chapters/`, then rebuild the EPUB only:

```bash
python make_book.py --skip-extract --skip-fetch \
  --workdir ./book --epub novel.epub --title "..." --author "..."
```

**Use your own cover.** Pass `--cover path/to/your.jpg`.

**Just get the URL list:**

```bash
python extract_urls.py --url "https://zetrotranslation.com/novel/<slug>/" > urls.txt
```

**Just rebuild an EPUB from existing markdown:**

```bash
python build_epub.py book/chapters/ --out novel.epub \
  --title "..." --author "..." --cover book/cover.jpg
```

## Things to know

The default 1-second delay between chapter fetches is intentionally polite. Bump it to `--delay 3` if you'd rather be slower; don't take it below ~0.5s.

This works for any site using the wp-manga / Madara WordPress theme, which is dozens of translation sites. The selectors that matter (`li.wp-manga-chapter`, `div.reading-content > div.text-left`, `.post-title`, `.author-content`) are baked into the theme and are unlikely to change.

If a translator on a different site adds a different kind of "please rate" block, add their wrapper class to `FLUFF_CLASSES` in `fetch_chapters.py`.

## License

Personal use. Don't redistribute scraped content. Buy the official release if there is one.
