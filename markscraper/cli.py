"""Command line entry point."""
from __future__ import annotations

import argparse
import pathlib
import sqlite3
import sys
from datetime import date, timedelta

from . import config, db, discover, parse
from .fetch import Fetcher, read_cache


def _pages(conn, since, until, limit, where=""):
    sql = "SELECT * FROM pages WHERE 1=1"
    args: list = []
    if since:
        sql += " AND (week_start IS NULL OR week_start >= ?)"
        args.append(f"{since}-01-01")
    if until:
        sql += " AND (week_start IS NULL OR week_start <= ?)"
        args.append(f"{until}-12-31")
    sql += where + " ORDER BY year_folder, filename"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql, args).fetchall()


def cmd_discover(args, conn):
    count = discover.sync(conn, Fetcher(rate=args.rate, refresh=args.refresh))
    print(f"discovered {count} pages")


def cmd_scrape(args, conn):
    fetcher = Fetcher(rate=args.rate, refresh=args.refresh)
    rows = _pages(conn, args.since, args.until, args.limit,
                  "" if args.refresh else " AND fetched_at IS NULL")
    print(f"fetching {len(rows)} pages at {args.rate}s intervals")
    ok = failed = 0
    for i, row in enumerate(rows, 1):
        try:
            res = fetcher.get(row["url"], row["etag"], row["last_modified"])
        except Exception as exc:                       # network/HTTP exhaustion
            conn.execute("UPDATE pages SET parse_error=? WHERE url=?",
                         (f"fetch: {exc}", row["url"]))
            failed += 1
            continue
        conn.execute(
            "UPDATE pages SET http_status=?, byte_size=?, content_sha256=?, "
            "etag=?, last_modified=?, fetched_at=datetime('now') WHERE url=?",
            (res.status, res.byte_size, res.sha256, res.etag,
             res.last_modified, row["url"]),
        )
        ok += 1
        if i % 50 == 0:
            conn.commit()
            print(f"  {i}/{len(rows)} ok={ok} failed={failed}", flush=True)
    conn.commit()
    print(f"fetched {ok}, failed {failed}")


def cmd_parse(args, conn):
    # Parsing never touches the network -- it reads only what scrape cached, so
    # the parser can be fixed and re-run freely.
    where = ""
    if not args.reparse:
        where += " AND parsed_at IS NULL"
    rows = _pages(conn, args.since, args.until, args.limit, where)
    print(f"parsing {len(rows)} cached pages")
    shows = segs = failed = skipped = 0
    for i, row in enumerate(rows, 1):
        try:
            html = read_cache(row["url"])
            if html is None:
                skipped += 1
                continue
            page = parse.parse_page(row["url"], html)
            s, g = db.store_page(conn, page)
            shows += s
            segs += g
        except Exception as exc:
            conn.execute("UPDATE pages SET parse_error=? WHERE url=?",
                         (f"parse: {exc}", row["url"]))
            conn.commit()
            failed += 1
        if i % 100 == 0:
            print(f"  {i}/{len(rows)} shows={shows} segments={segs}", flush=True)
    conn.commit()
    print(f"parsed {len(rows) - failed - skipped} pages -> {shows} shows, "
          f"{segs} segments"
          f"{f', {failed} failed' if failed else ''}"
          f"{f', {skipped} not cached' if skipped else ''}")


# Titles the site uses as a "not written yet" placeholder. Pages carrying one
# are re-checked on every update no matter how old they are.
PLACEHOLDER_SQL = (
    "s.title LIKE '%Coming Later This Friggin Week%' OR "
    "s.title LIKE '%Coverage Coming Later%' OR "
    "s.title LIKE 'Segment List Coming%'"
)


def cmd_update(args, conn):
    """Incremental refresh: new weeks, revised weeks, and filled-in placeholders.

    The site edits pages after publishing -- a week goes up with "Segment List
    Coming Later This Friggin Week" and is completed days later -- so looking
    only for new URLs would silently miss content. Recent and placeholder pages
    are revalidated with If-Modified-Since, which the server answers with a 304.
    """
    before = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
    discover.sync(conn, Fetcher(rate=args.rate, refresh=True))
    added = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0] - before

    if args.all:
        rows = conn.execute("SELECT * FROM pages ORDER BY year_folder, filename").fetchall()
    else:
        cutoff = (date.today() - timedelta(days=30 * args.months)).isoformat()
        rows = conn.execute(
            "SELECT * FROM pages WHERE fetched_at IS NULL OR week_start >= ? "
            "   OR url = ? "
            "   OR url IN (SELECT DISTINCT sh.page_url FROM segments s "
            "              JOIN shows sh ON sh.id = s.show_id "
            f"             WHERE {PLACEHOLDER_SQL}) "
            "ORDER BY year_folder, filename",
            (cutoff, config.CURRENT_PAGE),
        ).fetchall()

    print(f"{added} new page(s) in the index; revalidating {len(rows)}")
    fetcher = Fetcher(rate=args.rate)
    changed, unchanged, failed = [], 0, 0

    for row in rows:
        try:
            res = fetcher.get(row["url"], last_modified=row["last_modified"],
                              revalidate=True)
        except Exception as exc:
            conn.execute("UPDATE pages SET parse_error=? WHERE url=?",
                         (f"fetch: {exc}", row["url"]))
            failed += 1
            continue

        # A 304, or identical bytes, means there is nothing to re-parse.
        if res.status == 304 or res.sha256 == row["content_sha256"]:
            unchanged += 1
            # Record the validator even when nothing changed, so the next run
            # can ask with If-Modified-Since and get a 0-byte 304 back.
            conn.execute(
                "UPDATE pages SET fetched_at=datetime('now'), "
                "last_modified=COALESCE(?, last_modified) WHERE url=?",
                (res.last_modified, row["url"]),
            )
            continue

        conn.execute(
            "UPDATE pages SET http_status=?, byte_size=?, content_sha256=?, "
            "last_modified=?, fetched_at=datetime('now') WHERE url=?",
            (res.status, res.byte_size, res.sha256, res.last_modified, row["url"]),
        )
        changed.append(row["url"])
    conn.commit()

    shows = segs = 0
    for url in changed:
        html = read_cache(url)
        if html is None:
            continue
        try:
            a, b = db.store_page(conn, parse.parse_page(url, html))
            shows += a
            segs += b
        except Exception as exc:
            conn.execute("UPDATE pages SET parse_error=? WHERE url=?",
                         (f"parse: {exc}", url))
            failed += 1
    conn.commit()

    print(f"changed {len(changed)}, unchanged {unchanged}"
          f"{f', failed {failed}' if failed else ''}")
    if changed:
        print(f"re-parsed -> {shows} shows, {segs} segments")
        for url in changed[:10]:
            print(f"  {url.split('.com/')[1]}")
        if len(changed) > 10:
            print(f"  ... and {len(changed) - 10} more")
    if args.summarize:
        print("\nsummarising new segments...")
        from . import summarize
        args.resume = args.dry_run = args.sync = False
        args.chunk, args.wait = 5000, False
        args.limit = args.since = args.until = None
        summarize.run(args, conn)


def cmd_export(args, conn):
    """Write a compact, read-only-friendly copy of the archive.

    VACUUM INTO produces a clean single file; switching it off write-ahead
    logging means a reader with no write access to the directory can still open
    it, which is what the kiosk account needs.
    """
    target = pathlib.Path(args.to).expanduser()
    if target.exists():
        raise SystemExit(f"{target} already exists -- remove it or pick another path")
    target.parent.mkdir(parents=True, exist_ok=True)
    conn.execute("VACUUM INTO ?", (str(target),))

    copy = sqlite3.connect(target)
    copy.execute("PRAGMA journal_mode = DELETE")
    copy.close()
    target.chmod(0o444)
    size = target.stat().st_size / 1e6
    print(f"exported {size:,.0f} MB to {target} (read-only)")


def cmd_search(args, conn):
    sql = ("SELECT s.id, sh.show_date, s.title, s.air_time, s.summary, "
           "       snippet(segments_fts, 1, '[', ']', ' ... ', 12) AS snip "
           "FROM segments_fts f "
           "JOIN segments s ON s.id = f.rowid "
           "JOIN shows sh ON sh.id = s.show_id "
           "WHERE segments_fts MATCH ?")
    params: list = [args.query]
    if args.year:
        sql += " AND sh.show_date LIKE ?"
        params.append(f"{args.year}-%")
    sql += " ORDER BY bm25(segments_fts) LIMIT ?"
    params.append(args.limit)

    rows = conn.execute(sql, params).fetchall()
    if not rows:
        print("no matches")
        return
    for row in rows:
        when = row["show_date"] or "unknown date"
        time = f" {row['air_time']}" if row["air_time"] else ""
        print(f"\n[{row['id']}] {when}{time} — {row['title']}")
        if row["summary"]:
            print(f"    summary: {row['summary']}")
        elif row["snip"]:
            print(f"    ...{row['snip']}")
    print(f"\n{len(rows)} match(es)")


def cmd_stats(args, conn):
    q = lambda sql: conn.execute(sql).fetchone()[0]
    print(f"pages discovered : {q('SELECT COUNT(*) FROM pages')}")
    print(f"  fetched        : {q('SELECT COUNT(*) FROM pages WHERE fetched_at IS NOT NULL')}")
    print(f"  parsed         : {q('SELECT COUNT(*) FROM pages WHERE parsed_at IS NOT NULL')}")
    print(f"  errors         : {q('SELECT COUNT(*) FROM pages WHERE parse_error IS NOT NULL')}")
    for row in conn.execute("SELECT format, COUNT(*) c FROM pages "
                            "WHERE format IS NOT NULL GROUP BY format ORDER BY c DESC"):
        print(f"  format {row['format']:<8}: {row['c']}")
    print(f"shows            : {q('SELECT COUNT(*) FROM shows')}")
    print(f"  undated        : {q('SELECT COUNT(*) FROM shows WHERE show_date IS NULL')}")
    print(f"segments         : {q('SELECT COUNT(*) FROM segments')}")
    print(f"  with prose     : {q('SELECT COUNT(*) FROM segments WHERE body_text IS NOT NULL')}")
    print(f"  with air time  : {q('SELECT COUNT(*) FROM segments WHERE air_time IS NOT NULL')}")
    print(f"  summarised     : {q('SELECT COUNT(*) FROM segments WHERE summary IS NOT NULL')}")
    rng = conn.execute("SELECT MIN(show_date) a, MAX(show_date) b FROM shows "
                       "WHERE show_date IS NOT NULL").fetchone()
    print(f"date range       : {rng['a']} .. {rng['b']}")
    chars = q("SELECT COALESCE(SUM(body_chars),0) FROM segments")
    print(f"prose stored     : {chars/1e6:.1f}M chars (~{chars/4/1e6:.1f}M tokens)")


def cmd_summarize(args, conn):
    from . import summarize
    summarize.run(args, conn)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="markscraper",
                                 description="Archive marksfriggin.com into SQLite.")
    ap.add_argument("--db", default=None, help="database path")
    ap.add_argument("--rate", type=float, default=config.DEFAULT_RATE,
                    help="seconds between HTTP requests (default: 1.0)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("discover", help="build the page list from the index pages")
    p.add_argument("--refresh", action="store_true")
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("scrape", help="download pages to the local cache")
    p.add_argument("--since", type=int)
    p.add_argument("--until", type=int)
    p.add_argument("--limit", type=int)
    p.add_argument("--refresh", action="store_true", help="re-download cached pages")
    p.set_defaults(func=cmd_scrape)

    p = sub.add_parser("parse", help="parse cached pages into the database")
    p.add_argument("--since", type=int)
    p.add_argument("--until", type=int)
    p.add_argument("--limit", type=int)
    p.add_argument("--reparse", action="store_true", help="re-parse already-parsed pages")
    p.set_defaults(func=cmd_parse)

    p = sub.add_parser("update",
                       help="monthly refresh: new weeks, edits and placeholders")
    p.add_argument("--months", type=int, default=3,
                   help="how far back to revalidate (default: 3)")
    p.add_argument("--all", action="store_true",
                   help="revalidate every page in the archive")
    p.add_argument("--summarize", action="store_true",
                   help="summarise any new prose afterwards")
    p.add_argument("--model", default="claude-opus-5")
    p.add_argument("--effort", default="low",
                   choices=["low", "medium", "high", "xhigh", "max"])
    p.set_defaults(func=cmd_update)

    p = sub.add_parser("search", help="full-text search the archive")
    p.add_argument("query")
    p.add_argument("--year", type=int)
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("export", help="write a read-only copy for serving")
    p.add_argument("--to", required=True, help="destination path")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("stats", help="show archive coverage")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("summarize", help="generate summaries for segments")
    p.add_argument("--model", default="claude-opus-5")
    p.add_argument("--effort", default="low",
                   choices=["low", "medium", "high", "xhigh", "max"])
    p.add_argument("--since", type=int)
    p.add_argument("--until", type=int)
    p.add_argument("--limit", type=int)
    p.add_argument("--chunk", type=int, default=5000, help="requests per batch")
    p.add_argument("--dry-run", action="store_true", help="estimate cost only")
    p.add_argument("--sync", action="store_true", help="live calls instead of a batch")
    p.add_argument("--resume", action="store_true", help="poll in-flight batches")
    p.add_argument("--wait", action="store_true", help="poll until batches finish")
    p.set_defaults(func=cmd_summarize)

    args = ap.parse_args(argv)
    conn = db.connect(args.db)
    try:
        args.func(args, conn)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
