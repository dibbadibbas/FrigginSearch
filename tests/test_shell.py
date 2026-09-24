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


class TestSearchSyntax(unittest.TestCase):
    """The visitor-facing query language.

    Operators are honoured, but the expression is rebuilt from recognised
    tokens rather than passed through, so nothing a visitor types can become
    query syntax of its own.
    """

    def q(self, raw):
        return shell.parse_search(raw)

    # -- terms ---------------------------------------------------------
    def test_adjacent_words_are_anded(self):
        self.assertEqual(self.q("cabbie john").match, '"cabbie" AND "john"')

    def test_quoted_input_stays_a_phrase(self):
        self.assertEqual(self.q('"wrap up show"').match, '"wrap up show"')

    def test_trailing_star_keeps_prefix_search(self):
        self.assertEqual(self.q("beetle*").match, '"beetle"*')

    def test_apostrophes_survive(self):
        self.assertEqual(self.q("robin's").match, '"robin\'s"')

    # -- operators -----------------------------------------------------
    def test_operators_are_honoured_in_capitals(self):
        self.assertEqual(self.q("cabbie OR john").match, '"cabbie" OR "john"')
        self.assertEqual(self.q("cabbie NOT john").match, '"cabbie" NOT "john"')
        self.assertEqual(self.q("cabbie AND john").match, '"cabbie" AND "john"')

    def test_lowercase_operators_stay_searchable_words(self):
        # Otherwise "rock and roll" could not be searched for.
        self.assertEqual(self.q("rock and roll").match,
                         '"rock" AND "and" AND "roll"')

    def test_brackets_group(self):
        self.assertEqual(self.q("(cabbie OR john) AND fired").match,
                         '( "cabbie" OR "john" ) AND "fired"')

    # -- the IN clause -------------------------------------------------
    def test_in_year(self):
        parsed = self.q("cabbie IN 2001")
        self.assertEqual(parsed.match, '"cabbie"')
        self.assertEqual((parsed.date_from, parsed.date_to),
                         ("2001-01-01", "2001-12-31"))

    def test_in_year_range(self):
        parsed = self.q("cabbie AND john IN 2001-2003")
        self.assertEqual(parsed.match, '"cabbie" AND "john"')
        self.assertEqual((parsed.date_from, parsed.date_to),
                         ("2001-01-01", "2003-12-31"))

    def test_backwards_range_is_swapped(self):
        parsed = self.q("cabbie IN 2003-2001")
        self.assertEqual((parsed.date_from, parsed.date_to),
                         ("2001-01-01", "2003-12-31"))

    # -- errors are explained, never raised ----------------------------
    def test_malformed_queries_explain_themselves(self):
        for raw, fragment in [
            ("", "Nothing to search"),
            ("AND foo", "needs a word before"),
            ("a AND", "ends on an operator"),
            ("foo)", "Unmatched bracket"),
            ("(a", "Unclosed bracket"),
            ("()", "Empty brackets"),
            ("cabbie IN wednesday", "IN needs a year"),
            ("IN 2001", "Add a word"),
        ]:
            with self.subTest(raw=raw):
                parsed = self.q(raw)
                self.assertIsNone(parsed.match)
                self.assertIn(fragment, parsed.error)

    # -- containment ---------------------------------------------------
    def test_hostile_input_cannot_become_syntax(self):
        """Whatever a visitor types must run, and must not be syntax."""
        conn = db.connect(":memory:")
        conn.execute("INSERT INTO pages(url) VALUES ('u')")
        conn.execute("INSERT INTO shows(seq,page_url,show_date,day_header) "
                     "VALUES (0,'u','2020-06-01','h')")
        conn.execute("INSERT INTO segments(show_id,ordinal,title,body_text) "
                     "VALUES (1,0,'A Title','some prose here')")
        nasty = ['"', "''", "*", "a*b", "NEAR/2", "^bar", "col:x", "-",
                 "'; DROP TABLE segments; --", "segments_fts", "a\\b", "{}"]
        for raw in nasty:
            parsed = shell.parse_search(raw)
            if parsed.match is None:
                continue
            with self.subTest(raw=raw):
                conn.execute("SELECT rowid FROM segments_fts WHERE segments_fts "
                             "MATCH ?", (parsed.match,)).fetchall()
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM segments").fetchone()[0], 1)
        conn.close()

    def test_injection_attempt_becomes_plain_terms(self):
        parsed = self.q("'; DROP TABLE segments; --")
        self.assertEqual(parsed.match, '"DROP" AND "TABLE" AND "segments"')


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
