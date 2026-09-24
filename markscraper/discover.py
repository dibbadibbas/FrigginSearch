"""Build the list of weekly archive pages from the site's index pages."""
from __future__ import annotations

import re
from urllib.parse import urljoin

from . import config
from .fetch import Fetcher

# Week pages live in per-year folders: ../news20/6-1.htm, ../news96_97/dec-97.htm
WEEK_HREF = re.compile(r'href="([^"]*?(news[0-9_]+)/([^"/]+\.htm))"', re.I)


def week_urls(fetcher: Fetcher) -> list[tuple[str, str, str]]:
    """Return (url, year_folder, filename) for every weekly page, de-duplicated.

    Filenames are irregular before 1998 (mastertape86.htm, dec-97.htm), so these
    are always read from the index pages rather than constructed.
    """
    seen: dict[str, tuple[str, str, str]] = {}
    for index_url in config.INDEX_PAGES:
        page = fetcher.get(index_url)
        for href, folder, filename in WEEK_HREF.findall(page.text):
            url = urljoin(index_url, href)
            seen.setdefault(url, (url, folder.lower(), filename))
    return sorted(seen.values(), key=lambda r: (r[1], r[2]))


def sync(conn, fetcher: Fetcher) -> int:
    """Upsert discovered pages into the pages table. Returns the row count."""
    rows = week_urls(fetcher)
    conn.executemany(
        "INSERT INTO pages(url, year_folder, filename) VALUES (?,?,?) "
        "ON CONFLICT(url) DO UPDATE SET year_folder=excluded.year_folder, "
        "filename=excluded.filename",
        rows,
    )
    # The live page holds the current week before it reaches the archive index.
    conn.execute(
        "INSERT INTO pages(url, year_folder, filename) VALUES (?,?,?) "
        "ON CONFLICT(url) DO NOTHING",
        (config.CURRENT_PAGE, "current", "news.htm"),
    )
    conn.commit()
    return len(rows) + 1
