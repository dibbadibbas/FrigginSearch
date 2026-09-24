"""SQLite schema and helpers."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from . import config

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS pages (
  url            TEXT PRIMARY KEY,
  year_folder    TEXT,
  filename       TEXT,
  week_start     TEXT,
  week_end       TEXT,
  format         TEXT,
  http_status    INTEGER,
  byte_size      INTEGER,
  content_sha256 TEXT,
  etag           TEXT,
  last_modified  TEXT,
  fetched_at     TEXT,
  parsed_at      TEXT,
  parse_error    TEXT
);

CREATE TABLE IF NOT EXISTS shows (
  id              INTEGER PRIMARY KEY,
  seq             INTEGER,
  show_date       TEXT,
  weekday         TEXT,
  page_url        TEXT REFERENCES pages(url) ON DELETE CASCADE,
  day_header      TEXT,
  date_confidence TEXT,
  summary      TEXT,
  UNIQUE(page_url, seq)
);

CREATE TABLE IF NOT EXISTS segments (
  id              INTEGER PRIMARY KEY,
  show_id         INTEGER REFERENCES shows(id) ON DELETE CASCADE,
  ordinal         INTEGER,
  title           TEXT NOT NULL,
  air_time        TEXT,
  anchor          TEXT,
  body_text       TEXT,
  body_chars      INTEGER,
  source_url      TEXT,
  content_sha256  TEXT,
  scraped_at      TEXT,
  summary      TEXT,
  summary_model        TEXT,
  summary_effort       TEXT,
  summarized_at TEXT,
  batch_id        TEXT,
  UNIQUE(show_id, ordinal)
);

CREATE TABLE IF NOT EXISTS summary_batches (
  batch_id      TEXT PRIMARY KEY,
  model         TEXT,
  effort        TEXT,
  request_count INTEGER,
  status        TEXT,
  created_at    TEXT,
  retrieved_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_shows_date     ON shows(show_date);
CREATE INDEX IF NOT EXISTS idx_shows_page     ON shows(page_url);
CREATE INDEX IF NOT EXISTS idx_segments_show  ON segments(show_id);
CREATE INDEX IF NOT EXISTS idx_segments_sha   ON segments(content_sha256);
CREATE INDEX IF NOT EXISTS idx_segments_batch ON segments(batch_id);
CREATE INDEX IF NOT EXISTS idx_segments_todo
  ON segments(id) WHERE body_text IS NOT NULL AND summary IS NULL;

CREATE VIRTUAL TABLE IF NOT EXISTS segments_fts USING fts5(
  title, body_text, summary,
  content='segments', content_rowid='id',
  tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS segments_ai AFTER INSERT ON segments BEGIN
  INSERT INTO segments_fts(rowid, title, body_text, summary)
  VALUES (new.id, new.title, new.body_text, new.summary);
END;

CREATE TRIGGER IF NOT EXISTS segments_ad AFTER DELETE ON segments BEGIN
  INSERT INTO segments_fts(segments_fts, rowid, title, body_text, summary)
  VALUES ('delete', old.id, old.title, old.body_text, old.summary);
END;

CREATE TRIGGER IF NOT EXISTS segments_au AFTER UPDATE ON segments BEGIN
  INSERT INTO segments_fts(segments_fts, rowid, title, body_text, summary)
  VALUES ('delete', old.id, old.title, old.body_text, old.summary);
  INSERT INTO segments_fts(rowid, title, body_text, summary)
  VALUES (new.id, new.title, new.body_text, new.summary);
END;
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Open the database, creating the schema if needed."""
    path = Path(path) if path else config.DB_PATH
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def store_page(conn, page, fetched=None) -> tuple[int, int]:
    """Write a ParsedPage, replacing any previous parse of the same URL.

    Existing summaries are carried across by content hash, so the parser can be
    fixed and re-run without paying to regenerate them.
    Returns (show_count, segment_count).
    """
    preserved = {
        row["content_sha256"]: row
        for row in conn.execute(
            "SELECT s.content_sha256, s.summary, s.summary_model, s.summary_effort, "
            "       s.summarized_at, s.batch_id "
            "FROM segments s JOIN shows sh ON sh.id = s.show_id "
            "WHERE sh.page_url = ? AND s.summary IS NOT NULL",
            (page.url,),
        )
    }

    with conn:
        conn.execute("DELETE FROM shows WHERE page_url = ?", (page.url,))
        shows = segments = 0
        for show in page.shows:
            cur = conn.execute(
                "INSERT INTO shows(seq, show_date, weekday, page_url, day_header, "
                "date_confidence) VALUES (?,?,?,?,?,?)",
                (show.seq, show.show_date, show.weekday, page.url,
                 show.day_header, show.date_confidence),
            )
            show_id = cur.lastrowid
            shows += 1
            for seg in show.segments:
                sha = seg.content_sha256
                old = preserved.get(sha)
                conn.execute(
                    "INSERT INTO segments(show_id, ordinal, title, air_time, anchor, "
                    "body_text, body_chars, source_url, content_sha256, scraped_at, "
                    "summary, summary_model, summary_effort, summarized_at, batch_id) "
                    "VALUES (?,?,?,?,?,?,?,?,?,datetime('now'),?,?,?,?,?)",
                    (show_id, seg.ordinal, seg.title, seg.air_time, seg.anchor,
                     seg.body_text, seg.body_chars, page.url, sha,
                     old["summary"] if old else None,
                     old["summary_model"] if old else None,
                     old["summary_effort"] if old else None,
                     old["summarized_at"] if old else None,
                     old["batch_id"] if old else None),
                )
                segments += 1

        conn.execute(
            "UPDATE pages SET week_start=?, week_end=?, format=?, parsed_at=datetime('now'), "
            "parse_error=NULL" + (
                ", http_status=?, byte_size=?, content_sha256=?, etag=?, "
                "last_modified=?, fetched_at=datetime('now')" if fetched else ""
            ) + " WHERE url=?",
            (page.week_start, page.week_end, page.format)
            + ((fetched.status, fetched.byte_size, fetched.sha256,
                fetched.etag, fetched.last_modified) if fetched else ())
            + (page.url,),
        )
    return shows, segments
