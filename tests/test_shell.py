"""FrigginShell tests.

The shell is a login shell for an untrusted account, so the visitor-facing
input path is the part worth pinning down.
"""
import pathlib
import sqlite3
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from markscraper import db, shell


class TestFtsQuery(unittest.TestCase):
    def test_plain_words_become_quoted_and_anded(self):
        self.assertEqual(shell.fts_query("wrap up show"),
                         '"wrap" AND "up" AND "show"')

    def test_trailing_star_keeps_prefix_search(self):
        self.assertEqual(shell.fts_query("beetle*"), '"beetle"*')

    def test_quoted_input_is_kept_as_a_phrase(self):
        self.assertEqual(shell.fts_query('"wrap up show"'), '"wrap up show"')

    def test_apostrophes_survive(self):
        self.assertEqual(shell.fts_query("robin's"), '"robin\'s"')

    def test_empty_input_returns_none(self):
        for raw in ["", "   ", None, "!!!", "***"]:
            self.assertIsNone(shell.fts_query(raw), repr(raw))

    def test_fts_operators_are_neutralised(self):
        # None of these may reach the query planner as syntax.
        for raw in ['foo OR bar', 'foo NEAR bar', 'a AND (b OR c)', 'x"y', "a-b"]:
            query = shell.fts_query(raw)
            self.assertIsNotNone(query)
            self.assertNotIn("(", query)
            self.assertNotIn(")", query)

    def test_hostile_input_still_executes(self):
        """Whatever a visitor types must run without raising."""
        conn = db.connect(":memory:")
        conn.execute("INSERT INTO pages(url) VALUES ('u')")
        conn.execute("INSERT INTO shows(seq,page_url,show_date,day_header) "
                     "VALUES (0,'u','2020-06-01','h')")
        conn.execute("INSERT INTO segments(show_id,ordinal,title,body_text) "
                     "VALUES (1,0,'A Title','some prose here')")
        nasty = ['"', "''", "*", "a*b", "NEAR/2", "foo)", "-", "^bar",
                 "'; DROP TABLE segments; --", "col:x", "a OR b"]
        for raw in nasty:
            query = shell.fts_query(raw)
            if query is None:
                continue
            with self.subTest(raw=raw):
                conn.execute("SELECT rowid FROM segments_fts WHERE segments_fts "
                             "MATCH ?", (query,)).fetchall()
        # The table must still be there afterwards.
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM segments").fetchone()[0], 1)
        conn.close()


class TestReadOnly(unittest.TestCase):
    def test_archive_opens_read_only(self):
        import tempfile, os
        path = os.path.join(tempfile.mkdtemp(), "ro.db")
        seed = db.connect(path)
        seed.execute("INSERT INTO pages(url) VALUES ('u')")
        seed.commit()
        seed.close()

        conn = shell.open_db(path)
        with self.assertRaises(sqlite3.OperationalError):
            conn.execute("INSERT INTO pages(url) VALUES ('nope')")
        conn.close()


class TestFormatting(unittest.TestCase):
    def test_pretty_date(self):
        self.assertEqual(shell.pretty_date("2020-06-01"), "Monday, 1 June 2020")

    def test_pretty_date_tolerates_missing_and_bad_values(self):
        self.assertEqual(shell.pretty_date(None), "date unknown")
        self.assertEqual(shell.pretty_date("not-a-date"), "not-a-date")

    def test_wrap_preserves_paragraph_breaks(self):
        lines = shell.wrap("one\n\ntwo", "  ")
        self.assertIn("", lines)
        self.assertTrue(all(l == "" or l.startswith("  ") for l in lines))


if __name__ == "__main__":
    unittest.main(verbosity=2)
