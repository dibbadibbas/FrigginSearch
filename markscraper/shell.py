"""FrigginShell -- a restricted, menu-driven front end to the archive.

Designed to be the login shell for an unprivileged account, so it is
deliberately a dead end: it never starts a subprocess, never shells out to a
pager (less would let a visitor type "!sh" and escape), never opens a file path
the visitor supplies, and holds the database open read-only. Exiting the menu
ends the session.
"""
from __future__ import annotations

import os
import random
import re
import shutil
import signal
import sqlite3
import sys
import textwrap
from datetime import date

from . import config

APP = "FrigginShell"

# ---------------------------------------------------------------- presentation

def _use_colour() -> bool:
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR")


class Ink:
    def __init__(self, enabled: bool):
        self.on = enabled

    def _w(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.on else text

    def head(self, t):  return self._w("1;36", t)
    def key(self, t):   return self._w("1;33", t)
    def dim(self, t):   return self._w("2", t)
    def bold(self, t):  return self._w("1", t)
    def hit(self, t):   return self._w("1;32", t)


def width() -> int:
    return max(48, min(shutil.get_terminal_size((80, 24)).columns, 100))


def height() -> int:
    return max(10, shutil.get_terminal_size((80, 24)).lines)


def rule(char: str = "-") -> str:
    return char * width()


def pretty_date(iso: str | None) -> str:
    if not iso:
        return "date unknown"
    try:
        d = date.fromisoformat(iso)
    except ValueError:
        return iso
    return d.strftime("%A, %d %B %Y").replace(" 0", " ")


def wrap(text: str, indent: str = "") -> list[str]:
    out: list[str] = []
    for para in (text or "").split("\n"):
        para = para.strip()
        if not para:
            out.append("")
            continue
        out.extend(textwrap.wrap(para, width() - len(indent),
                                 initial_indent=indent, subsequent_indent=indent)
                   or [""])
    return out


# ---------------------------------------------------------------------- search

SEARCH_HELP = """
  cabbie john                 both words, anywhere in the segment
  cabbie OR john              either word
  cabbie NOT john             the first, but not the second
  "wrap up show"              an exact phrase
  beetle*                     anything starting with beetle
  (cabbie OR john) AND fired  brackets to group
  cabbie AND john IN 2001     one year only
  cabbie AND john IN 2001-2003  a range of years

  AND, OR, NOT and IN must be capitals. Results run oldest first."""

# Only these shapes are recognised; anything else becomes a plain search term,
# so visitor input is never handed to the query planner as syntax.
TOKEN = re.compile(r'"[^"]*"|\(|\)|[^\s()"]+')
OPERATORS = {"AND", "OR", "NOT"}
YEARS = re.compile(r"(\d{4})(?:-(\d{4}))?$")
FTS_SAFE = re.compile(r"[^\w' ]+", re.UNICODE)


class Search:
    """A parsed query: an FTS5 expression plus an optional date window."""

    def __init__(self, match=None, date_from=None, date_to=None, error=None):
        self.match = match
        self.date_from = date_from
        self.date_to = date_to
        self.error = error


def _term(raw: str) -> str | None:
    """Quote one word or phrase so it can only ever be a search term."""
    prefix = raw.endswith("*")
    if raw.startswith('"'):
        prefix = False
        raw = raw.strip('"')
    cleaned = FTS_SAFE.sub(" ", raw).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    # A token of pure punctuation ("'", "--") is not worth searching for.
    if not cleaned or not re.search(r"\w", cleaned):
        return None
    return f'"{cleaned}"' + ("*" if prefix else "")


def parse_search(raw: str) -> Search:
    """Turn visitor input into a safe FTS5 expression and a date window.

    The expression is rebuilt from recognised tokens rather than passed
    through, so punctuation and stray syntax cannot reach SQLite. Operators
    are honoured only in capitals, which keeps a lowercase "and" searchable
    as an ordinary word.
    """
    raw = (raw or "").strip()
    if not raw:
        return Search(error="Nothing to search for.")

    tokens = TOKEN.findall(raw)

    # Pull "IN 2001" / "IN 2001-2003" out before anything else.
    date_from = date_to = None
    if "IN" in tokens:
        at = tokens.index("IN")
        span = tokens[at + 1] if at + 1 < len(tokens) else ""
        years = YEARS.match(span)
        if not years:
            return Search(error="IN needs a year, like IN 2001 or IN 2001-2003.")
        first, second = years.group(1), years.group(2) or years.group(1)
        if first > second:
            first, second = second, first
        date_from, date_to = f"{first}-01-01", f"{second}-12-31"
        tokens = tokens[:at] + tokens[at + 2:]

    # Rebuild the expression, inserting the implicit AND between adjacent terms.
    parts: list[str] = []
    depth = 0
    previous = "start"          # start | term | operator | open | close
    for token in tokens:
        if token == "(":
            if previous in ("term", "close"):
                parts.append("AND")
            parts.append("(")
            depth += 1
            previous = "open"
        elif token == ")":
            depth -= 1
            if depth < 0:
                return Search(error="Unmatched bracket.")
            if previous in ("start", "operator", "open"):
                return Search(error="Empty brackets.")
            parts.append(")")
            previous = "close"
        elif token in OPERATORS:
            if previous in ("start", "operator", "open"):
                return Search(error=f"{token} needs a word before it.")
            parts.append(token)
            previous = "operator"
        else:
            term = _term(token)
            if term is None:
                continue
            if previous in ("term", "close"):
                parts.append("AND")
            parts.append(term)
            previous = "term"

    if depth:
        return Search(error="Unclosed bracket.")
    if previous == "operator":
        return Search(error="The search ends on an operator.")
    if not parts:
        if date_from:
            return Search(error="Add a word to search for as well as a year.")
        return Search(error="Nothing to search for.")

    return Search(" ".join(parts), date_from, date_to)


def fts_query(raw: str) -> str | None:
    """The expression half of a parsed query, or None if it cannot be built."""
    return parse_search(raw).match


# ------------------------------------------------------------------------ data

def open_db(path=None) -> sqlite3.Connection:
    """Open the archive read-only, falling back to a query_only connection."""
    path = str(path or os.environ.get("MARKSCRAPER_DB") or config.DB_PATH)
    if not os.path.exists(path):
        raise SystemExit(f"{APP}: archive not found at {path}")
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.execute("SELECT 1 FROM segments LIMIT 1")
    except sqlite3.Error:
        # A write-ahead-log database can refuse a read-only open; fall back to a
        # normal handle that the database itself refuses to let us write to.
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA query_only = ON")
    conn.row_factory = sqlite3.Row
    return conn


# The site posts a placeholder day ahead of writing it up; landing on one of
# those makes a poor front page, so they are skipped when choosing "latest".
PLACEHOLDER = "%Coming Later This Friggin Week%"


def latest_show(conn):
    """The most recent show that actually has something to read."""
    real = conn.execute(
        "SELECT sh.id, sh.show_date, sh.weekday, sh.summary FROM shows sh "
        "WHERE sh.show_date IS NOT NULL AND sh.show_date <= date('now') "
        "  AND EXISTS (SELECT 1 FROM segments s WHERE s.show_id = sh.id "
        "              AND s.title NOT LIKE ?) "
        "ORDER BY sh.show_date DESC, sh.id DESC LIMIT 1", (PLACEHOLDER,)
    ).fetchone()
    if real:
        return real
    return conn.execute(
        "SELECT id, show_date, weekday, summary FROM shows "
        "WHERE show_date IS NOT NULL ORDER BY show_date DESC, id DESC LIMIT 1"
    ).fetchone()


def show_segments(conn, show_id):
    return conn.execute(
        "SELECT id, title, air_time, body_text, body_chars, summary "
        "FROM segments WHERE show_id=? ORDER BY ordinal", (show_id,)
    ).fetchall()


# ----------------------------------------------------------------------- views

class FrigginShell:
    RESULT_LIMIT = 200

    def __init__(self, conn):
        self.conn = conn
        self.ink = Ink(_use_colour())
        self.last_results: list[sqlite3.Row] = []

    # -- input helpers ----------------------------------------------------
    def ask(self, prompt: str) -> str:
        try:
            return input(prompt).strip()
        except EOFError:
            raise Quit from None
        except KeyboardInterrupt:
            print()
            return ""

    def pause(self) -> None:
        self.ask(self.ink.dim("  [enter] "))

    def page(self, lines: list[str]) -> None:
        """Internal pager. Never shells out -- less would allow an escape."""
        room = height() - 3
        shown = 0
        for line in lines:
            print(line)
            shown += 1
            if shown >= room:
                answer = self.ask(self.ink.dim("  -- more -- [enter, q] "))
                if answer.lower().startswith("q"):
                    return
                shown = 0

    # -- screens ----------------------------------------------------------
    def banner(self) -> None:
        stats = self.conn.execute(
            "SELECT COUNT(*) segs, MIN(show_date) a, MAX(show_date) b "
            "FROM segments JOIN shows ON shows.id = segments.show_id "
            "WHERE show_date IS NOT NULL"
        ).fetchone()
        print()
        print(self.ink.head(f"  {APP}"))
        print(self.ink.dim(f"  The Stern Show archive  ·  {stats['segs']:,} segments  ·  "
                           f"{(stats['a'] or '?')[:4]}-{(stats['b'] or '?')[:4]}"))
        print(rule("="))

    def render_latest(self) -> None:
        show = latest_show(self.conn)
        if not show:
            print("  The archive is empty.")
            return
        segments = show_segments(self.conn, show["id"])
        print()
        print(self.ink.bold(f"  Latest show — {pretty_date(show['show_date'])}"))
        print(rule())

        if show["summary"]:
            for line in wrap(show["summary"], "  "):
                print(line)
            print()

        lines: list[str] = []
        for seg in segments:
            when = (seg["air_time"] or "").rjust(8)
            lines.append(f"  {self.ink.dim(when)}  {seg['title']}")
            if seg["summary"]:
                lines.extend(wrap(seg["summary"], "            "))
        self.page(lines)

        written = sum(1 for s in segments if s["body_chars"])
        note = (f"  {len(segments)} segment(s)"
                + (f", {written} with a write-up" if written
                   else " — titles only, no write-up published"))
        print(self.ink.dim(note))
        if written:
            print(self.ink.dim("  Use [2] to search, or [3] to open this date in full."))

    def menu(self) -> None:
        k = self.ink.key
        print()
        print(rule())
        print(f"  {k('1')} Latest show    {k('2')} Search        {k('3')} Browse a date")
        print(f"  {k('4')} Random segment {k('5')} Archive stats {k('q')} Quit")

    # -- actions ----------------------------------------------------------
    def do_search(self) -> None:
        print()
        print(self.ink.bold("  Search the archive"))
        print(self.ink.dim(SEARCH_HELP))
        raw = self.ask("\n  Search: ")
        query = parse_search(raw)
        if query.error:
            print(f"  {query.error}")
            return

        sql = ("SELECT s.id, s.title, s.air_time, sh.show_date, "
               "       snippet(segments_fts, 1, '<<', '>>', ' … ', 14) AS snip "
               "FROM segments_fts f "
               "JOIN segments s ON s.id = f.rowid "
               "JOIN shows sh ON sh.id = s.show_id "
               "WHERE segments_fts MATCH ?")
        params: list = [query.match]
        if query.date_from:
            sql += " AND sh.show_date >= ? AND sh.show_date <= ?"
            params += [query.date_from, query.date_to]
        # Oldest first, so a run of results reads as the story unfolding.
        sql += " ORDER BY sh.show_date IS NULL, sh.show_date, s.ordinal LIMIT ?"
        params.append(self.RESULT_LIMIT + 1)

        try:
            rows = self.conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            print("  That search could not be run. Try plain words.")
            return

        if not rows:
            where = f" in {query.date_from[:4]}-{query.date_to[:4]}" if query.date_from else ""
            print(f"  No matches for {raw!r}{where}.")
            return

        truncated = len(rows) > self.RESULT_LIMIT
        rows = rows[: self.RESULT_LIMIT]
        self.last_results = rows

        lines = [""]
        for i, row in enumerate(rows, 1):
            lines.append(f"  {self.ink.key(str(i).rjust(3))}  "
                         f"{(row['show_date'] or '????-??-??')}  {row['title']}")
            if row["snip"]:
                snip = row["snip"].replace("<<", "\033[1;32m" if self.ink.on else "[") \
                                  .replace(">>", "\033[0m" if self.ink.on else "]")
                lines.extend(wrap(snip, "       "))
        self.page(lines)

        span = ""
        if rows[0]["show_date"] and rows[-1]["show_date"]:
            span = f", {rows[0]['show_date'][:4]} to {rows[-1]['show_date'][:4]}"
        note = (f"  Showing the oldest {len(rows)}{span} — narrow it with IN, "
                f"or add another word."
                if truncated else f"  {len(rows)} result(s){span}.")
        print(self.ink.dim(note))
        print(self.ink.dim("  Enter a number to read one."))
        self.open_result()

    def open_result(self) -> None:
        if not self.last_results:
            return
        choice = self.ask("  Read which? [number, enter to skip] ")
        if not choice.isdigit():
            return
        index = int(choice)
        if not 1 <= index <= len(self.last_results):
            print("  No result with that number.")
            return
        self.render_segment(self.last_results[index - 1]["id"])

    def render_segment(self, segment_id: int) -> None:
        row = self.conn.execute(
            "SELECT s.title, s.air_time, s.body_text, s.summary, sh.show_date "
            "FROM segments s JOIN shows sh ON sh.id = s.show_id WHERE s.id=?",
            (segment_id,)
        ).fetchone()
        if not row:
            return
        lines = ["", f"  {self.ink.bold(row['title'])}",
                 self.ink.dim(f"  {pretty_date(row['show_date'])}"
                              + (f"  ·  {row['air_time']}" if row["air_time"] else "")),
                 rule()]
        if row["summary"]:
            lines.extend(wrap(row["summary"], "  "))
            lines.append("")
        if row["body_text"]:
            lines.extend(wrap(row["body_text"], "  "))
        elif not row["summary"]:
            lines.append(self.ink.dim("  No write-up was published for this segment."))
        self.page(lines)

    def do_browse(self) -> None:
        raw = self.ask("\n  Date or year [YYYY-MM-DD or YYYY]: ")
        if re.fullmatch(r"\d{4}", raw):
            rows = self.conn.execute(
                "SELECT sh.id, sh.show_date, COUNT(s.id) n FROM shows sh "
                "LEFT JOIN segments s ON s.show_id = sh.id "
                "WHERE sh.show_date LIKE ? GROUP BY sh.id "
                "ORDER BY sh.show_date LIMIT 400", (f"{raw}-%",)
            ).fetchall()
            if not rows:
                print(f"  Nothing archived for {raw}.")
                return
            self.page([""] + [f"  {r['show_date']}   {r['n']:3d} segment(s)" for r in rows])
            print(self.ink.dim(f"  {len(rows)} show(s) in {raw}."))
            return

        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            print("  Enter a year (2004) or a full date (2004-06-15).")
            return

        shows = self.conn.execute(
            "SELECT id, show_date, summary FROM shows WHERE show_date=? ORDER BY id",
            (raw,)).fetchall()
        if not shows:
            print(f"  No show archived for {raw}.")
            return
        for show in shows:
            segments = show_segments(self.conn, show["id"])
            lines = ["", f"  {self.ink.bold(pretty_date(show['show_date']))}", rule()]
            self.last_results = segments
            for i, seg in enumerate(segments, 1):
                when = (seg["air_time"] or "").rjust(8)
                lines.append(f"  {self.ink.key(str(i).rjust(3))} {self.ink.dim(when)}  "
                             f"{seg['title']}")
            self.page(lines)
        print(self.ink.dim("  Enter a number to read one."))
        self.open_result()

    def do_random(self) -> None:
        row = self.conn.execute(
            "SELECT id FROM segments WHERE body_text IS NOT NULL "
            "ORDER BY RANDOM() LIMIT 1").fetchone()
        if not row:
            row = self.conn.execute(
                "SELECT id FROM segments ORDER BY RANDOM() LIMIT 1").fetchone()
        if row:
            self.render_segment(row["id"])

    def do_stats(self) -> None:
        q = lambda sql: self.conn.execute(sql).fetchone()[0]
        rng = self.conn.execute(
            "SELECT MIN(show_date) a, MAX(show_date) b FROM shows "
            "WHERE show_date IS NOT NULL").fetchone()
        print()
        print(f"  {'Shows archived':<22}{q('SELECT COUNT(*) FROM shows'):>12,}")
        print(f"  {'Segments':<22}{q('SELECT COUNT(*) FROM segments'):>12,}")
        print(f"  {'With a write-up':<22}"
              f"{q('SELECT COUNT(*) FROM segments WHERE body_text IS NOT NULL'):>12,}")
        print(f"  {'With a summary':<22}"
              f"{q('SELECT COUNT(*) FROM segments WHERE summary IS NOT NULL'):>12,}")
        chars = q("SELECT COALESCE(SUM(body_chars),0) FROM segments")
        print(f"  {'Words held':<22}{chars // 6:>12,}")
        print(f"  {'Earliest show':<22}{rng['a'] or '-':>12}")
        print(f"  {'Latest show':<22}{rng['b'] or '-':>12}")

    # -- loop -------------------------------------------------------------
    def run(self) -> None:
        self.banner()
        self.render_latest()
        actions = {
            "1": self.render_latest, "2": self.do_search, "3": self.do_browse,
            "4": self.do_random, "5": self.do_stats,
        }
        while True:
            self.menu()
            choice = self.ask("  > ").lower()
            if choice in ("q", "quit", "exit", "bye"):
                raise Quit
            action = actions.get(choice)
            if action:
                try:
                    action()
                except KeyboardInterrupt:
                    print()
            elif choice:
                print("  Pick 1-5, or q to leave.")


class Quit(Exception):
    """Visitor asked to leave."""


def main(argv=None) -> int:
    # A kiosk session should not be suspendable or interruptible into anything.
    for sig in (getattr(signal, "SIGTSTP", None), getattr(signal, "SIGQUIT", None)):
        if sig is not None:
            try:
                signal.signal(sig, signal.SIG_IGN)
            except (OSError, ValueError):
                pass

    conn = open_db()
    shell = FrigginShell(conn)

    # Without a terminal (ssh host "command") there is no menu to drive.
    if not sys.stdin.isatty():
        shell.banner()
        shell.render_latest()
        return 0

    try:
        shell.run()
    except Quit:
        pass
    except KeyboardInterrupt:
        pass
    finally:
        conn.close()
    print(f"\n  Thanks for stopping by. Bye!\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
