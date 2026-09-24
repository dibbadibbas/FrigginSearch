# MarkScraper

Archives the Howard Stern Show recaps published on marksfriggin.com into a local
SQLite database with full-text search, and can generate short
summaries for each segment.

## Setup

```bash
python3 -m venv .venv
curl -sS https://bootstrap.pypa.io/get-pip.py | .venv/bin/python   # if ensurepip is missing
.venv/bin/pip install -r requirements.txt
```

## Usage

```bash
alias markscraper='.venv/bin/python -m markscraper.cli'

markscraper discover                 # build the page list (~1,530 weekly pages)
markscraper scrape                   # download to data/cache at 1 req/sec
markscraper parse                    # parse the cache into data/marksfriggin.db
markscraper stats                    # coverage report
markscraper search "wrap up show"    # full-text search
markscraper search "beetlejuice" --year 2004

markscraper update                   # monthly incremental refresh
```

`scrape` and `parse` are deliberately separate. Scraping writes raw HTML to
`data/cache/`; parsing only ever reads from there. That means the parser can be
changed and re-run over the whole archive without touching the network:

```bash
markscraper parse --reparse
```

Re-parsing is idempotent — it replaces each page's rows in place, and carries
existing summaries across by content hash so you never pay to regenerate the
same text twice.

## Keeping it current

```bash
markscraper update              # run this monthly
markscraper update --months 6   # look further back
markscraper update --all        # revalidate all 1,530 pages
```

`update` is not just "fetch new weeks". The site edits pages after publishing —
a week goes up with *"Segment List Coming Later This Friggin Week"* and is
completed days later — so three things are re-checked every run:

1. **New weeks** — the two index pages are re-read for URLs we have never seen.
2. **Recent weeks** — everything with a `week_start` in the last `--months`
   (default 3).
3. **Placeholder pages** — any page still carrying a "coming later" segment,
   however old.

Each is revalidated with `If-Modified-Since`. The server has no `ETag` but does
honour `Last-Modified`, answering with a **0-byte 304** when nothing changed, so
a monthly run costs ~45 conditional requests and almost no bandwidth. Only pages
whose bytes actually changed are re-parsed, and re-parsing preserves existing
summaries.

Add `--summarize` to summarise any newly-arrived prose in the same run.

## FrigginShell

A restricted, menu-driven front end to the archive, meant to be the login shell
for an unprivileged account. Someone connects and lands on the latest show, then
searches from a menu:

```bash
ssh friggin@your-host
```

```
  FrigginShell
  The Stern Show archive  ·  70,532 segments  ·  1987-2026
================================================================================

  Latest show — Tuesday, 22 September 2026
--------------------------------------------------------------------------------
    7:00am  Martha Reeves
    7:15am  Gary Double Books Jewel & Jenny McCarthy
    ...

  1 Latest show    2 Search        3 Browse a date
  4 Random segment 5 Archive stats q Quit
```

Run it locally either way — the launcher resolves its own location, so it works
from a checkout as well as from an installed copy:

```bash
./bin/frigginshell
.venv/bin/python -m markscraper.shell
```

### Installing it on a server

```bash
sudo ./install/install-frigginshell.sh
```

That creates the `friggin` account, installs the application under
`/opt/markscraper`, exports a read-only copy of the archive, and writes an sshd
rule. Then give the account a way in — an `authorized_keys` file, or a password
for an open guest login. The script prints both options when it finishes.

### How it stays restricted

The shell is a deliberate dead end, because a login shell for a shared account
is a real attack surface:

- **No subprocesses, ever.** In particular it never pipes to `less`, which would
  let a visitor type `!sh` and walk out into a real shell. Paging is done in
  Python.
- **The archive is opened read-only**, so a visitor cannot modify it even if the
  file permissions were wrong.
- **Search input is neutralised** before it reaches SQLite — every term is
  quoted, so punctuation and FTS operators cannot become query syntax.
- **`SIGTSTP` and `SIGQUIT` are ignored**, so the session cannot be suspended or
  dumped out of.
- **`ForceCommand`** covers `ssh friggin@host <command>`, which setting a login
  shell alone would not, and forwarding of every kind is switched off.
- Exiting the menu ends the SSH session.

To serve a standalone copy of the archive, `markscraper export --to PATH` writes
a compacted, read-only file with write-ahead logging turned off, which is what a
reader with no write access to the directory needs.

## Summaries

Summarising is a separate phase, so the archive lands first and you decide later
how much of it to spend money on.

```bash
markscraper summarize --dry-run                      # project tokens and cost
markscraper summarize --limit 5 --sync               # spot-check a handful live
markscraper summarize --model claude-haiku-4-5       # submit the backfill as a batch
markscraper summarize --resume                       # collect finished batches
```

The backfill uses the Batch API (half price). Segments are claimed with their
batch ID before submission, so an interrupted run is recovered with `--resume`
rather than re-submitted. `--since` / `--until` / `--limit` narrow the work set.

Rough backfill cost for the 52,692 prose segments, batch-priced:
`haiku-4-5` ≈ $37, `sonnet-5` ≈ $110, `opus-5` ≈ $185. Run `--dry-run` for a
real figure against your own data.

## What the site looks like

Two index pages list every weekly archive page: `old96-05.htm` (1986–2005) and
`old-news.htm` (2006–present). Weekly URLs are read from those pages, never
constructed — weeks do not align to fixed dates and pre-1998 filenames are
irregular (`mastertape86.htm`, `dec-97.htm`, `12-6-99.htm`).

Pages come in two shapes, and which one you get varies week to week rather than
by era:

- **recap** — a day banner per weekday followed by `<li><b>Title</b>` segments
  with full write-ups. This is most of 1986 through September 2023.
- **listing** — the same page skeleton, but the day sections hold only a
  standing note and the real content is the table of contents: segment titles
  and air times, no prose. This is every week from late 2023, plus rerun and
  vacation weeks throughout.

`parse.py` decides per page by comparing how many prose segments a page yields
against the size of its table of contents.

Other things the parser handles: day banners changed colour (`#ff0000` in the
90s, `#006699` later) and comma placement; 1998 weeks run Friday to Monday;
1996–97 pages hold a whole month; and undated `Wrap Up Show` banners are folded
into the day above them rather than becoming dateless shows.

## Schema

`pages` → `shows` → `segments`, plus a `segments_fts` FTS5 index kept in sync by
triggers and a `summary_batches` table tracking in-flight batch jobs. Segment
prose is stored verbatim in `body_text`; generated summaries go in `summary`
alongside the model and effort used.

## Notes

- The site has no `robots.txt`. The scraper is rate limited to 1 request/second
  and caches everything, so a re-run costs nothing.
- It rejects short User-Agent strings with HTTP 406; a full browser UA is sent.
- Pages are cp1252, not UTF-8, and say so nowhere.
- The content belongs to the site's author. This builds a personal local
  archive; don't redistribute what it collects.

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -v
```
