# ahobby-novel-to-epub

Scrape a novel from asianhobbyist.com and bundle a chosen chapter range as an EPUB. Same four-script architecture as the other site pipelines, adapted for this site — which hosts **partial** fan translations, so picking a range matters.

## Quick start

```bash
pip install requests beautifulsoup4 ebooklib markdown

# A specific range (inclusive) — the common case here:
python make_book.py \
  --toc-url "https://www.asianhobbyist.com/series/<novel-slug>/" \
  --from 231 --to 274 \
  --workdir ./book \
  --epub novel.epub
```

Leave off `--from`/`--to` to grab everything the site has translated.

## The four scripts

| Script | What it does | Standalone? |
|---|---|---|
| `extract_urls.py` | Reads `Ch. N` labels, filters by `--from`/`--to`, emits URLs | Yes |
| `fetch_chapters.py` | Downloads each chapter, strips ads/donation/nav, → Markdown | Yes |
| `build_epub.py` | Bundles a directory of `chapter-NNN.md` files into an EPUB | Yes |
| `make_book.py` | Runs all three with a range filter and auto-detected metadata | Yes |

All four must sit in the same directory (`make_book.py` imports from the others).

## What's site-specific to asianhobbyist.com

**It's a partial translation with gaps.** Only some segments of the novel are done — for the sample novel that's chapters 29–55, then 229, then 231–260, then 261.2–274. So chapter numbers are not contiguous, and you usually want a slice. The `--from` / `--to` filter (inclusive, decimals allowed) is the main tool. When you run it, the gap report on stderr tells you exactly which numbers in your requested span don't exist, e.g.:

```
Found 71 chapters total; 43 selected in range [231, 274]; gaps (untranslated/absent): 260-261 → book/urls.txt
```

**Two inconsistent URL slug schemes.** Older chapters live at `.../the-world-is-overflowing-with-monster-chapter-29/`, newer ones at `.../overflowing-231/`. You can't build a URL from a chapter number — so the extractor reads the real `href` off each `Ch. N` link instead of guessing.

**Decimal / split chapters.** Some chapters are numbered like `261.2`. These get filenames like `chapter-261.2.md` and sort into the right slot (between 260 and 262) in the EPUB.

**Ads and donation blocks woven into the prose.** Each chapter page has `code-block` ad slots, "Click Donate" / Patreon / Ko-fi links, an announcement panel, chapter-picker nav rows, and inline ad/comment scripts (aclib, Vuukle). All stripped by class fingerprint; the actual prose lives in a nested `div.markdown-main-panel`.

**Range-annotated title.** Because you're usually grabbing a partial range, the EPUB title gets the range appended automatically — e.g. `The World Is Overflowing with Monster… (Ch. 231-274)` — so a partial book is self-describing in your library. The identifier includes the range too, so different ranges of the same novel don't collide. Pass `--title` to override.

## Common workflows

**Resume after a failed run.** Rerun the same command — existing chapters are skipped.

**Force a re-fetch.** Pass `--overwrite`.

**Just the URL list for a range:**

```bash
python extract_urls.py --url "<series-url>" --from 231 --to 274 > urls.txt
```

**Hand-edit chapters before binding, then rebuild EPUB only:**

```bash
python make_book.py --skip-extract --skip-fetch \
  --workdir ./book --epub novel.epub --title "..."
```

**Use your own cover:** `--cover path/to/your.jpg`.

## Things to know

The default 1-second delay between fetches is polite; bump with `--delay 2` if you like. There's usually no author listed on these fan-translation pages, so the author field defaults to "Unknown" — pass `--author` if you want one.

## License

Personal use. Don't redistribute scraped content. These are unpaid fan translations — support the translators (the donation links the scraper strips out are real; visit the site if you want to chip in) and buy any official release that exists.
