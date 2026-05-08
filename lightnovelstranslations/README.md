# lnt-novel-to-epub

Scrape a novel from lightnovelstranslations.com and bundle it as an EPUB. Same four-script architecture as the other site pipelines, but adapted for this site's custom theme.

## Quick start

```bash
pip install requests beautifulsoup4 ebooklib markdown

# Drop all four .py files in the same folder, then:
python make_book.py \
  --toc-url "https://lightnovelstranslations.com/novel/<novel-slug>/" \
  --workdir ./book \
  --epub novel.epub
```

That's it. ~300 chapters at the default 1-second delay finishes in about 5 minutes.

## The four scripts

| Script | What it does | Standalone? |
|---|---|---|
| `extract_urls.py` | Parses the chapter list, preserves DOM order as reading order | Yes |
| `fetch_chapters.py` | Downloads each chapter, strips ads / nav placeholders, → Markdown | Yes |
| `build_epub.py` | Bundles a directory of `chapter-NNN.md` files into an EPUB | Yes |
| `make_book.py` | Runs all three with auto-detected metadata | Yes |

`make_book.py` imports the work-horse functions from the other three, so all four files must sit in the same directory.

## What's site-specific to lightnovelstranslations.com

**DOM order is the reading order.** The site has decimal "side" chapters (33.5, 47.5, …), split chapters (250-1, 250-2), and chapters that appear out of numeric order (section 251-300 lists 252 before 251). Sorting by chapter number would scramble the translator's intended sequence. The pipeline uses position-based filenames (`chapter-001.md`, `chapter-002.md`, …) so the EPUB reads in the order the translator laid out, including the prologue, side chapters, and decimals at their right places.

**Inline ads woven into the prose.** Each chapter has 4 ad blocks (`<div class="adv_content_N ads_content ads-section">`) sprinkled between paragraphs, plus `<ins class="adsbygoogle">` slots, plus `<script>` and `<iframe>` elements. All filtered by class fingerprint. You won't see any "support our sponsors" leakage.

**Failed-shortcode navigation placeholders.** The chapter pages have `<div id="textbox">` with literal `[previous_page]` / `[next_page]` template tokens that didn't render — the WordPress shortcodes failed at some point and the strings stayed in the HTML. Stripping `#textbox` removes them all in one go.

**No Open Graph tags.** Unlike the other two sites, this one has no `og:title` / `og:image` meta. Metadata comes from custom class hooks: `.novel_title`, `.novel_text`, and `.novel_detail_info` (which is a flat string like `Author: あまうい白一 Translator: Weslykan Editor: …` that gets parsed into a dict).

**Some novels have no cover.** When a novel has no uploaded cover, the theme renders a `no-image.png` placeholder. The cover detector skips that placeholder plus other theme assets (logos, decorative `krystal-pack-N.png` images), and falls back to no-cover rather than embedding the placeholder in your EPUB. Pass `--cover path/to/your.jpg` to supply one yourself.

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

**Use your own cover.** Pass `--cover path/to/your.jpg` (essential for novels without uploaded covers).

**Just get the URL list:**

```bash
python extract_urls.py --url "https://lightnovelstranslations.com/novel/<slug>/" > urls.txt
```

**Just rebuild an EPUB from existing markdown:**

```bash
python build_epub.py book/chapters/ --out novel.epub \
  --title "..." --author "..." --cover book/cover.jpg
```

## Things to know

The default 1-second delay between chapter fetches is intentionally polite. For 300+ chapter novels you might want to bump it to `--delay 2` to be friendlier; don't take it below ~0.5s.

The author field auto-detects to whatever's in the `.novel_detail_info` block, which is usually the original Japanese name (e.g., あまうい白一). Pass `--author "Romanized Name"` if you'd rather have the romanization in your library view.

## License

Personal use. Don't redistribute scraped content. Translation work on this site is unpaid; buy any official English release that exists.
