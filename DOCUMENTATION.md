# MarkScraper — project documentation

A local archive of the Howard Stern Show recaps published on marksfriggin.com,
parsed into SQLite with full-text search and an optional summarisation phase.

**Status:** complete and populated. Full archive crawled, parsed, and indexed.

---

## 1. What was built

| | |
|---|---|
| Pages crawled | **1,530** — 0 fetch failures, 0 parse errors |
| Shows | **8,030** (15 undated, 0.2%) |
| Segments | **70,566** |
| — with full write-ups | **52,692** |
| — with air times | **66,336** |
| Date range | **1987-01-28 → 2026-09-25** |
| Prose stored | 203.6M characters (~50.9M tokens) |
| Database | 358 MB (`data/marksfriggin.db`) |
| HTML cache | 226 MB (`data/cache/`) |
| Page formats | 1,152 recap · 378 listing |
| Tests | 19, all passing |

Coverage by decade:

| Decade | Shows | Segments | With prose |
|---|---|---|---|
| 1980s | 26 | 217 | 217 |
| 1990s | 1,120 | 4,994 | 4,990 |
| 2000s | 2,650 | 27,100 | 25,132 |
| 2010s | 2,440 | 24,332 | 19,819 |
| 2020s | 1,779 | 13,889 | 2,503 |

The sharp prose drop in the 2020s is the source, not the scraper: the site's
author stopped writing detailed recaps around September 2023. Everything after
that is segment titles and air times only.

---

## 2. What the site actually is

None of this is documented anywhere; it was established by crawling and reading
the markup. It is recorded here because the parser depends on all of it.

### Entry points

| Page | Covers | Week links |
|---|---|---|
| `old96-05.htm` | 1986–2005 | 451 |
| `old-news.htm` | 2006–present | 1,079 |
| `news.htm` | the current in-progress week | — |

Weekly pages live at `/news{YY}/{file}.htm`. **URLs are always read from the
index pages, never constructed** — weeks do not align to fixed dates, and early
filenames are irregular:

```
news20/6-1.htm            news98/1-5-98.htm
news96_97/dec-97.htm      news99/12-6-99.htm
news86/mastertape86.htm
```

Guessing URLs was the single biggest source of wasted effort during exploration;
most apparent "404s" were invented filenames.

### Three page formats

Format varies **week to week**, not by era, so it is detected per page.

**1. Recap** — a day banner per weekday, then segments with full write-ups.
Roughly 1987 through September 2023. 150–380 KB/page.

```html
<font size=+1 color="#006699"><u><b><i>-- Monday, June 1, 2020 --</i></b></u></font>
<ul>
<p><li><b>Segment Title</b><br>
prose...<P>more prose...
```

**2. Listing** — the same page skeleton, but the day sections carry only a
standing note; the real content is the table of contents. Every week from late
2023, plus rerun and vacation weeks throughout. 6–22 KB/page.

```html
<TR><!--  MON  --><td valign=top><font size=-1><ul>
<li>Segment Title. 09/21/26. 7:00am</li>
```

**3. Flat** — 1996 pages have **no day banners at all**. Each segment carries
its own date, sometimes inline after the title, sometimes inside the title.

```html
<p><li><b>Segment Title</b> 4-30-96. prose...
<p><li><b>Another Title. 6/28/96.</b><br>prose...
```

This third format was discovered only after the full crawl — 7 pages had come
back as `format='unknown'`. It is the reason `parse` reads from cache and can be
re-run for free.

### Era-specific quirks the parser handles

- **Banner colour changed**: `#ff0000` in the 1990s, `#006699` later. The colour
  is deliberately not part of the match.
- **Comma placement varies**: `-- Monday, June 1, 2020 --` vs
  `-- Friday January 9, 1998 --`.
- **1998 weeks run backwards** — Friday appears first, Monday last.
- **1996–97 pages hold a whole month**, not a week (December 1997 → 24 shows).
- **Not every banner is a day.** `Wrap Up Show`, `The Wrap Up Show`, and various
  promo blocks use identical markup. Undated banners are folded into the day
  above them rather than becoming dateless shows.
- **Replay banners are real but out of range** — a 1998 page can legitimately
  contain a `Friday, February 23, 2007` section, and `news86/mastertape86.htm`
  is dated 2007 because it documents a Master Tape Theatre replay of 1986
  material. These dates are kept as-is.
- **The table of contents exists in both eras**, which is where recap-format
  segments get their air times from.

### Transport quirks

- **No `robots.txt`** — the URL 404s. No stated crawl rules; the scraper is
  rate-limited to 1 req/sec regardless.
- **HTTP 406 on short User-Agent strings.** `"Mozilla/5.0"` alone is rejected;
  a full browser UA works. Several early probe failures traced to this.
- **Encoding is cp1252**, declared nowhere. UTF-8 decoding corrupts punctuation.
- **No `ETag`.** `If-None-Match` returns a full 200. `Last-Modified` *is* sent
  and `If-Modified-Since` correctly returns a **0-byte 304** — this is what makes
  monthly updates cheap.
- **Pages are edited after publication**, which is why `update` revalidates
  rather than only looking for new URLs.

---

## 3. Architecture

```
markscraper/
  config.py      constants: URLs, User-Agent, encoding, paths, pricing
  fetch.py       polite cached HTTP + conditional revalidation
  discover.py    index pages -> the work list of weekly URLs
  parse.py       format detection + the three parsers
  db.py          schema, FTS triggers, idempotent storage
  summarize.py   batch/sync summarisation
  cli.py         argparse entry point
  shell.py       FrigginShell -- restricted menu front end
bin/frigginshell launcher for a login shell / ForceCommand
install/        server install script
tests/
  fixtures/      saved pages from four eras
  test_parse.py  19 tests
data/
  cache/         raw HTML, mirrors the site's own paths
  marksfriggin.db
```

### The important design decision

**Scraping and parsing are strictly separate.** `scrape` writes raw HTML to
`data/cache/`; `parse` only ever reads from there and never touches the network.

This paid for itself repeatedly. The parser was rewritten three times during the
build — once for the Wrap Up Show merging, once for the 1996 flat format, once
for a schema change — and each full re-parse of all 1,530 pages took about two
minutes and zero requests.

### Idempotency

`db.store_page()` replaces a page's rows inside a single transaction. Re-parsing
never duplicates. Existing summaries are carried across by
`content_sha256` of title + body, so improving the parser does not mean paying
to regenerate the same text again.

---

## 4. Database schema

`pages` → `shows` → `segments`, plus an FTS5 index and batch tracking.

```sql
pages       url PK, year_folder, filename, week_start, week_end, format,
            http_status, byte_size, content_sha256, etag, last_modified,
            fetched_at, parsed_at, parse_error

shows       id PK, seq, show_date, weekday, page_url FK, day_header,
            date_confidence, summary
            UNIQUE(page_url, seq)

segments    id PK, show_id FK, ordinal, title, air_time, anchor,
            body_text, body_chars, source_url, content_sha256, scraped_at,
            summary, summary_model, summary_effort, summarized_at, batch_id
            UNIQUE(show_id, ordinal)

segments_fts  FTS5(title, body_text, summary)
              external content, porter unicode61, trigger-synced

summary_batches  batch_id PK, model, effort, request_count, status,
                 created_at, retrieved_at
```

Notes:

- `shows` is keyed on `(page_url, seq)`, not `(page_url, show_date, day_header)`.
  The original key collided on 5 pages that repeat a banner within one page.
- `date_confidence` is `exact` (parsed from a banner), `inferred` (derived from
  a segment's own date or the week range), or `unknown`.
- `body_text` is `NULL` for listing-format segments — there is no prose to store.
- FTS5 stays in sync through `AFTER INSERT/UPDATE/DELETE` triggers; verified in
  tests.

---

## 5. Command reference

```bash
alias markscraper='.venv/bin/python -m markscraper.cli'
```

| Command | Purpose |
|---|---|
| `discover` | Read both index pages, build the work list (~1,530 rows) |
| `scrape` | Download pages to `data/cache/` at 1 req/sec |
| `parse` | Parse the cache into the database (no network) |
| `update` | Incremental refresh — see below |
| `search "…"` | FTS5 search, bm25-ranked, with snippets |
| `stats` | Coverage report |
| `export` | Write a read-only copy for serving |
| `summarize` | Generate summaries |

Global: `--db PATH`, `--rate SECONDS`.
Filters on `scrape`/`parse`: `--since YYYY`, `--until YYYY`, `--limit N`.
`parse --reparse` re-parses everything; `scrape --refresh` re-downloads.

### First-time build

```bash
markscraper discover && markscraper scrape && markscraper parse && markscraper stats
```

The crawl takes ~25 minutes at 1 req/sec; the parse ~2 minutes.

---

## 6. Monthly update

```bash
markscraper update                 # the routine monthly run
markscraper update --months 6      # look further back
markscraper update --all           # revalidate all 1,530 pages
markscraper update --summarize     # also summarise anything new
```

`update` deliberately does more than look for new weeks, because the site
**edits pages after publishing** — a week appears with *"Segment List Coming
Later This Friggin Week"* and is completed days later. 32 pages currently carry
such a placeholder. Looking only for new URLs would silently miss that content.

Each run re-checks three sets:

1. **New weeks** — both index pages are re-read for unseen URLs.
2. **Recent weeks** — any page with `week_start` inside `--months` (default 3).
3. **Placeholder pages** — any page still carrying a "coming later" segment,
   regardless of age.

Every candidate is revalidated with `If-Modified-Since`. Unchanged pages return
a 0-byte 304; changed pages are re-downloaded, re-parsed, and their summaries
preserved. A typical run touches ~45 pages and reports:

```
0 new page(s) in the index; revalidating 45
changed 0, unchanged 45
```

Because the server sends no `ETag`, the first `update` after a database rebuild
downloads its candidates in full to compare hashes, then stores `Last-Modified`
so later runs get true 304s.

To automate, add to `crontab -e`:

```cron
0 6 1 * * .venv/bin/python -m markscraper.cli update >> /tmp/markscraper-update.log 2>&1
```

---

## 7. FrigginShell

A restricted, menu-driven front end to the archive, intended as the login shell
for an unprivileged account so that `ssh friggin@host` lands someone on the
latest show and a search menu.

```bash
.venv/bin/python -m markscraper.shell     # run it locally
sudo ./install/install-frigginshell.sh    # install it on a server
```

The installer creates the account, copies the application to `/opt/markscraper`,
exports a read-only archive, and writes `/etc/ssh/sshd_config.d/60-frigginshell.conf`.
Authentication is left to the admin — an `authorized_keys` file for known users,
or a password for an open guest login.

### Menu

| Key | Action |
|---|---|
| `1` | Latest show — titles, air times, summaries where present |
| `2` | Search — FTS5, bm25-ranked, with highlighted snippets |
| `3` | Browse a date — a full date (`2004-06-15`) or a whole year (`2004`) |
| `4` | Random segment |
| `5` | Archive stats |
| `q` | Quit, which ends the SSH session |

The landing screen deliberately skips the site's placeholder days — it shows the
most recent show that actually has something to read, not the "coming later"
stub that is often the newest row.

### Containment

A login shell on a shared account is an attack surface, so the program is built
as a dead end:

- **No subprocesses at all.** It never pipes output to `less`, which would let a
  visitor type `!sh` and escape into a real shell. Paging is implemented in
  Python.
- **The database is opened read-only** (`mode=ro`, falling back to a
  `query_only` handle), so the archive cannot be modified through it. Covered by
  a test that asserts an `INSERT` raises.
- **Search input never becomes query syntax.** `fts_query()` quotes every term,
  so punctuation and FTS5 operators cannot reach the query planner. Tested
  against a set of hostile inputs, each of which must execute without raising
  and leave the tables intact.
- **`SIGTSTP` and `SIGQUIT` are ignored**, so the session cannot be suspended or
  dumped out of.
- **`ForceCommand`** is used rather than only a login shell, because it also
  covers `ssh friggin@host <command>`. Agent, X11, TCP, tunnel and stream-local
  forwarding are all disabled, along with `PermitUserRC`.
- Without a TTY the program prints the latest show and exits rather than trying
  to run a menu.

### Serving a standalone copy

```bash
markscraper export --to /opt/markscraper/data/marksfriggin.db
```

`VACUUM INTO` writes a compacted single file, then write-ahead logging is turned
off and the file is set to mode 444. A reader with no write access to the
directory cannot open a WAL database, which is why this step exists.

---

## 8. Summarisation

A deliberately separate phase, so the archive exists first and the spend is a
later decision.

```bash
markscraper summarize --dry-run                  # projection from real data
markscraper summarize --limit 5 --sync           # spot-check quality
markscraper summarize --model claude-haiku-4-5   # batch backfill
markscraper summarize --resume                   # collect finished batches
```

- **Batch by default** (half price). Requests are chunked (`--chunk`, default
  5,000) and each segment is stamped with its `batch_id` *before* submission, so
  an interrupted run is recovered with `--resume` rather than re-submitted.
  Results are keyed by `custom_id`, never by position.
- **`--sync`** issues live calls, for small incremental runs.
- **`--dry-run`** samples 25 real segments through `count_tokens` and
  extrapolates, rather than guessing.
- **`--model`** defaults to `opus-5`; **`--effort`** defaults to `low`,
  which suits extractive condensation. `effort` is omitted automatically for
  models that reject it.
- Work queue is `body_text IS NOT NULL AND summary IS NULL AND batch_id IS NULL`.

Backfill scale: **52,692 prose segments, ~50.9M input tokens.** Batch-priced
estimate — `haiku-4-5` ≈ $37, `sonnet-5` ≈ $110, `opus-5` ≈ $185. Run `--dry-run` for
a real figure.

**Not yet run.** No API credentials are configured on this machine; the command
exits with a clear message until `ANTHROPIC_API_KEY` is set.

---

## 9. Known limitations

- **15 undated shows** (0.2%). Mostly promo blocks appearing before any dated
  banner, plus a few 1996 segments whose date could not be pinned. Their
  segments are stored; only `show_date` is missing.
- **A few 1996 edge cases** — `aug-96.htm` collapses to a single date, and
  `mar-96.htm` contains one stray out-of-range date. Content is captured either
  way.
- **Out-of-range replay dates are intentional**, not bugs (see §2).
- **Post-2023 has no prose**, so there is nothing for the summariser to do
  there. This is a property of the source.
- **`etag` column is vestigial** — the server never sends one. Kept so the
  schema does not need changing if that ever starts.
- **No incremental FTS rebuild command.** If the FTS index is ever suspected of
  drift, `parse --reparse` rebuilds everything.

---

## 10. Environment notes

Python 3.14 on Ubuntu 26.04. SQLite 3.46.1 with FTS5.

This machine had no `pip` and no `ensurepip`, and `python3 -m venv` therefore
produced a venv with no `pip` inside it. Resolved by bootstrapping:

```bash
python3 -m venv .venv
curl -sS https://bootstrap.pypa.io/get-pip.py | .venv/bin/python
.venv/bin/pip install -r requirements.txt
```

Dependencies: `anthropic`, `requests`, `beautifulsoup4`, `lxml`. `lxml` matters —
the markup is malformed enough that strict parsers drift.

Run the tests with:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

---

## 11. Legal

The content belongs to the site's author. This is a personal local archive:
rate-limited, cached so nothing is fetched twice, and not redistributed.
