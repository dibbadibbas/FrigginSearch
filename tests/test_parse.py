"""Parser tests against saved pages from each era of the site."""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from markscraper import db, parse

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"


def load(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


class TestTitleSplitting(unittest.TestCase):
    def test_strips_trailing_date_and_time(self):
        title, iso, air = parse.split_title("Gary's Family Tree. 06/01/20. 9:35am")
        self.assertEqual(title, "Gary's Family Tree")
        self.assertEqual(iso, "2020-06-01")
        self.assertEqual(air, "9:35am")

    def test_two_digit_year_splits_at_the_eighties(self):
        self.assertEqual(parse.split_title("A. 1/9/98. 6:25am")[1], "1998-01-09")
        self.assertEqual(parse.split_title("A. 1/9/20.")[1], "2020-01-09")

    def test_title_without_a_suffix_is_untouched(self):
        title, iso, air = parse.split_title("In The News")
        self.assertEqual((title, iso, air), ("In The News", None, None))

    def test_entities_are_decoded(self):
        self.assertEqual(parse.split_title("Sal &amp; Richard")[0], "Sal & Richard")


class TestRecapFormat(unittest.TestCase):
    """A 2020 week: five days, full prose under each."""

    @classmethod
    def setUpClass(cls):
        cls.page = parse.parse_page("http://x/news20/6-1.htm",
                                    load("recap-2020-06-01.htm"))

    def test_detected_as_recap(self):
        self.assertEqual(self.page.format, "recap")

    def test_week_range(self):
        self.assertEqual(self.page.week_start, "2020-06-01")
        self.assertEqual(self.page.week_end, "2020-06-05")

    def test_five_consecutive_weekdays(self):
        self.assertEqual([s.show_date for s in self.page.shows],
                         ["2020-06-01", "2020-06-02", "2020-06-03",
                          "2020-06-04", "2020-06-05"])
        self.assertTrue(all(s.date_confidence == "exact" for s in self.page.shows))

    def test_every_segment_has_prose(self):
        segments = [sg for sh in self.page.shows for sg in sh.segments]
        self.assertEqual(len(segments), 29)
        self.assertTrue(all(sg.body_chars > 200 for sg in segments))

    def test_air_times_come_from_the_contents_table(self):
        segments = [sg for sh in self.page.shows for sg in sh.segments]
        self.assertTrue(all(sg.air_time for sg in segments))

    def test_titles_carry_no_trailing_date(self):
        for show in self.page.shows:
            for seg in show.segments:
                self.assertNotRegex(seg.title, r"\d{1,2}/\d{1,2}/\d{2}\s*$")


class TestListingFormat(unittest.TestCase):
    """A 2026 week: segment titles only, no write-ups."""

    @classmethod
    def setUpClass(cls):
        cls.page = parse.parse_page("http://x/news26/9-21.htm",
                                    load("listing-2026-09-21.htm"))

    def test_detected_as_listing(self):
        self.assertEqual(self.page.format, "listing")

    def test_no_segment_has_prose(self):
        segments = [sg for sh in self.page.shows for sg in sh.segments]
        self.assertEqual(len(segments), 28)
        self.assertTrue(all(sg.body_text is None for sg in segments))

    def test_dates_cover_the_week(self):
        self.assertEqual([s.show_date for s in self.page.shows],
                         ["2026-09-21", "2026-09-22", "2026-09-23",
                          "2026-09-24", "2026-09-25"])


class TestEarlyPages(unittest.TestCase):
    """Pre-2000 pages: red day banners, no comma, days in reverse order."""

    def test_1998_week_parses_in_reverse_order(self):
        page = parse.parse_page("http://x/news98/1-5-98.htm",
                                load("recap-1998-01-05.htm"))
        self.assertEqual(page.format, "recap")
        dates = [s.show_date for s in page.shows]
        self.assertIn("1998-01-09", dates)
        self.assertIn("1998-01-05", dates)
        self.assertTrue(all(d for d in dates), "no show should be undated")

    def test_1997_month_page_yields_a_show_per_day(self):
        page = parse.parse_page("http://x/news96_97/dec-97.htm",
                                load("month-1997-12.htm"))
        self.assertEqual(page.format, "recap")
        self.assertEqual(len(page.shows), 24)
        self.assertTrue(all(s.show_date.startswith("1997-12") for s in page.shows))


class TestWrapUpMerging(unittest.TestCase):
    def test_undated_banners_do_not_create_dateless_shows(self):
        for name in ["recap-2020-06-01.htm", "recap-1998-01-05.htm",
                     "month-1997-12.htm", "listing-2026-09-21.htm"]:
            page = parse.parse_page("http://x/" + name, load(name))
            undated = [s for s in page.shows if s.show_date is None]
            self.assertEqual(undated, [], f"{name} produced dateless shows")


class TestStorage(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        self.url = "http://x/news20/6-1.htm"
        self.conn.execute("INSERT INTO pages(url) VALUES (?)", (self.url,))
        self.page = parse.parse_page(self.url, load("recap-2020-06-01.htm"))

    def tearDown(self):
        self.conn.close()

    def test_reparse_is_idempotent_and_keeps_summaries(self):
        self.assertEqual(db.store_page(self.conn, self.page), (5, 29))
        self.conn.execute("UPDATE segments SET summary='kept' WHERE ordinal=0")
        self.conn.commit()

        self.assertEqual(db.store_page(self.conn, self.page), (5, 29))
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM segments").fetchone()[0], 29)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM segments WHERE summary='kept'"
            ).fetchone()[0], 5)

    def test_full_text_index_tracks_segments(self):
        db.store_page(self.conn, self.page)
        indexed = self.conn.execute("SELECT COUNT(*) FROM segments_fts").fetchone()[0]
        self.assertEqual(indexed, 29)
        # A term from a known segment title must be findable.
        hits = self.conn.execute(
            "SELECT COUNT(*) FROM segments_fts WHERE segments_fts MATCH 'howard'"
        ).fetchone()[0]
        self.assertGreater(hits, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestUpdateSelection(unittest.TestCase):
    """The placeholder matcher decides which old pages get re-checked forever,
    so it must catch the site's real placeholders without dragging in ordinary
    titles that merely contain the word 'coming'."""

    MATCHES = [
        "Segment List Coming Later This Friggin Week.",
        "Coming Later This Friggin Week.",
        "Wrap Up Show Coverage Coming Later",
    ]
    NON_MATCHES = [
        "Coming Up",
        "Coming Up Today",
        "What's Coming Up",
        "Robin's Upcoming ''Millionaire'' Appearance",
        "In The News",
    ]

    def setUp(self):
        from markscraper import cli
        self.conn = db.connect(":memory:")
        self.conn.execute("INSERT INTO pages(url) VALUES ('u')")
        self.conn.execute(
            "INSERT INTO shows(seq, page_url, show_date, day_header) "
            "VALUES (0,'u','2026-09-21','h')")
        for i, title in enumerate(self.MATCHES + self.NON_MATCHES):
            self.conn.execute(
                "INSERT INTO segments(show_id, ordinal, title) VALUES (1,?,?)",
                (i, title))
        self.sql = ("SELECT s.title FROM segments s JOIN shows sh ON sh.id=s.show_id "
                    f"WHERE {cli.PLACEHOLDER_SQL}")

    def tearDown(self):
        self.conn.close()

    def test_matches_real_placeholders_only(self):
        found = {r["title"] for r in self.conn.execute(self.sql)}
        self.assertEqual(found, set(self.MATCHES))
        self.assertTrue(found.isdisjoint(self.NON_MATCHES))
