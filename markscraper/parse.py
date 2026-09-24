"""Parse weekly archive pages into shows and segments.

The markup is hand-maintained HTML from 1996 onward and is malformed by modern
standards (unclosed <p>, <li>, <td>). Rather than trust a DOM tree, the page is
sliced with regexes on the raw HTML and BeautifulSoup is used only to turn each
fragment into clean text.

Every page has the same broad shape:

  <h3>For the week of MM/DD/YYYY to MM/DD/YYYY</h3>
  <Table> ... <!--  MON  --> <td> <ul><li>Title. MM/DD/YY. 7:00am</li>...  </Table>
  <font size=+1 color="..."> -- Monday, June 1, 2020 -- </font>
  <ul><p><li><b>Segment Title</b><br> prose...<P>prose... </ul>

The table of contents carries air times; the day sections carry the prose. In the
recap era both are populated. From late 2023 the day sections hold only a standing
note and the table of contents is the only real content.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date

from bs4 import BeautifulSoup

MONTHS = {
    m.lower(): i
    for i, m in enumerate(
        ["January", "February", "March", "April", "May", "June", "July",
         "August", "September", "October", "November", "December"], 1)
}
MONTHS.update({m[:3]: i for m, i in list(MONTHS.items())})

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday",
            "saturday", "sunday"]

# Day banner. The colour changed over the years (#ff0000 in the 90s, #006699
# later) so it is deliberately not part of the match.
DAY_HEADER = re.compile(
    r"<font\s+size=\+1[^>]*>(?:\s*<[^>]+>)*\s*--\s*(?P<text>[^<>-]{3,80}?)\s*--",
    re.I)

# "-- Monday, June 1, 2020 --" and "-- Friday January 9, 1998 --"
DAY_DATE = re.compile(
    r"(?P<weekday>" + "|".join(WEEKDAYS) + r")\s*,?\s+"
    r"(?P<month>[A-Za-z]+)\s+(?P<day>\d{1,2})\s*,?\s*(?P<year>\d{4})", re.I)

# A segment inside a day section.
SEGMENT = re.compile(r"<li>\s*<b>(?P<title>.*?)</b>\s*(?:<br\s*/?>)?", re.I | re.S)

# Table-of-contents cells: <!--  MON  --> ... <td> ... </td>
TOC_CELL = re.compile(
    r"<!--\s*(?P<day>MON|TUE|WED|THU|FRI|SAT|SUN)\s*-->(?P<body>.*?)(?=<!--\s*(?:MON|TUE|WED|THU|FRI|SAT|SUN)\s*-->|</table>)",
    re.I | re.S)
TOC_ITEM = re.compile(r"<li>(?P<text>.*?)(?=<li>|</ul>|</td>|$)", re.I | re.S)

# Trailing ". 06/01/20. 7:00am" / ". 1/9/98. 6:25am" on a title.
TITLE_SUFFIX = re.compile(
    r"\.?\s*(?P<date>\d{1,2}/\d{1,2}/\d{2,4})\.?\s*"
    r"(?P<time>\d{1,2}:\d{2}\s*[ap]\.?m\.?)?\s*$", re.I)

WEEK_RANGE = re.compile(
    r"<h3[^>]*>.*?week\s+of\s+(?P<start>[\d/]+)\s+to\s+(?P<end>[\d/]+)",
    re.I | re.S)


@dataclass
class Segment:
    title: str
    ordinal: int
    air_time: str | None = None
    anchor: str | None = None
    body_text: str | None = None
    title_date: str | None = None

    @property
    def body_chars(self) -> int:
        return len(self.body_text or "")

    @property
    def content_sha256(self) -> str:
        payload = f"{self.title}\x00{self.body_text or ''}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class Show:
    seq: int
    show_date: str | None
    weekday: str | None
    day_header: str
    date_confidence: str
    segments: list[Segment] = field(default_factory=list)


@dataclass
class ParsedPage:
    url: str
    format: str
    week_start: str | None
    week_end: str | None
    shows: list[Show] = field(default_factory=list)

    @property
    def segment_count(self) -> int:
        return sum(len(s.segments) for s in self.shows)


def clean_text(fragment: str) -> str:
    """Turn an HTML fragment into readable text, keeping paragraph breaks."""
    fragment = re.sub(r"<\s*/?\s*(?:p|br|div)\s*[^>]*>", "\n", fragment, flags=re.I)
    text = BeautifulSoup(fragment, "lxml").get_text(" ")
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\s*\n\s*", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _parse_loose_date(raw: str) -> str | None:
    """Parse 06/01/20, 1/9/98 or 09/21/2026 into an ISO date."""
    try:
        month, day, year = (int(p) for p in raw.split("/"))
    except ValueError:
        return None
    if year < 100:
        # The archive starts in 1986, so two-digit years split at the 80s.
        year += 1900 if year >= 80 else 2000
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def split_title(raw_title: str) -> tuple[str, str | None, str | None]:
    """Split a trailing date/time off a title. Returns (title, iso_date, time)."""
    title = clean_text(raw_title)
    match = TITLE_SUFFIX.search(title)
    if not match:
        return title, None, None
    stripped = title[: match.start()].rstrip(" .")
    iso = _parse_loose_date(match.group("date"))
    air_time = match.group("time")
    if air_time:
        air_time = re.sub(r"\s+", "", air_time).lower()
    # Guard against a title that is *only* a date.
    return (stripped or title), iso, air_time


def parse_toc(html: str) -> dict[str, list[tuple[str, str | None, str | None]]]:
    """Read the per-weekday table of contents.

    Returns {weekday_abbrev: [(title, iso_date, air_time), ...]}.
    """
    toc: dict[str, list[tuple[str, str | None, str | None]]] = {}
    for cell in TOC_CELL.finditer(html):
        day = cell.group("day").lower()
        items = []
        for item in TOC_ITEM.finditer(cell.group("body")):
            text = clean_text(item.group("text"))
            if not text:
                continue
            title, iso, air_time = split_title(text)
            if title:
                items.append((title, iso, air_time))
        if items:
            toc.setdefault(day, []).extend(items)
    return toc


def _day_sections(html: str) -> list[tuple[str, str]]:
    """Slice the page into (header_text, body_html) per day banner."""
    matches = list(DAY_HEADER.finditer(html))
    sections = []
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(html)
        sections.append((match.group("text").strip(), html[match.end():end]))
    return sections


def _segments_from_section(body_html: str) -> list[Segment]:
    """Extract <li><b>Title</b><br>prose segments from one day section."""
    segments = []
    matches = list(SEGMENT.finditer(body_html))
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body_html)
        raw_body = body_html[match.end():end]
        title, title_date, air_time = split_title(match.group("title"))
        if not title:
            continue
        anchor = None
        preceding = body_html[max(0, match.start() - 300):match.start()]
        names = re.findall(r'<a\s+name="([^"]+)"', preceding, re.I)
        if names:
            anchor = names[-1]
        segments.append(Segment(
            title=title, ordinal=len(segments), air_time=air_time,
            anchor=anchor, body_text=clean_text(raw_body) or None,
            title_date=title_date,
        ))
    return segments


# 1996 pages carry no day banners at all: each segment is
# "<li><b>Title</b> 4-30-96. prose" with a dash-separated date inline.
INLINE_DATE = re.compile(r"^\s*(?P<date>\d{1,2}[-/]\d{1,2}[-/]\d{2,4})\.?\s*")


def parse_flat(html: str) -> list[Show]:
    """Parse pages that list dated segments with no day banners (1996)."""
    by_date: dict[str | None, Show] = {}
    for seg in _segments_from_section(html):
        iso = seg.title_date
        if iso is None and seg.body_text:
            match = INLINE_DATE.match(seg.body_text)
            if match:
                iso = _parse_loose_date(match.group("date").replace("-", "/"))
                if iso:
                    seg.body_text = seg.body_text[match.end():].strip() or None
        show = by_date.get(iso)
        if show is None:
            weekday = (WEEKDAYS[date.fromisoformat(iso).weekday()] if iso else None)
            show = Show(len(by_date), iso, weekday, iso or "undated",
                        "exact" if iso else "unknown", [])
            by_date[iso] = show
        seg.ordinal = len(show.segments)
        show.segments.append(seg)
    return sorted(by_date.values(), key=lambda s: (s.show_date or "9999"))


def parse_page(url: str, html: str) -> ParsedPage:
    week_start = week_end = None
    week = WEEK_RANGE.search(html)
    if week:
        week_start = _parse_loose_date(week.group("start"))
        week_end = _parse_loose_date(week.group("end"))

    toc = parse_toc(html)
    sections = _day_sections(html)

    shows: list[Show] = []
    prose_segments = 0
    for header, body_html in sections:
        segments = _segments_from_section(body_html)
        prose_segments += sum(1 for s in segments if s.body_chars > 200)

        iso = weekday = None
        confidence = "unknown"
        match = DAY_DATE.search(header)
        if match:
            month = MONTHS.get(match.group("month").lower()[:3])
            if month:
                try:
                    iso = date(int(match.group("year")), month,
                               int(match.group("day"))).isoformat()
                    weekday = match.group("weekday").lower()
                    confidence = "exact"
                except ValueError:
                    iso = None
        if iso is None and shows:
            # An undated banner is a sub-section of the day above it -- the
            # "Wrap Up Show" blocks that follow most weekdays. Fold its
            # segments into that show rather than inventing a dateless one.
            previous = shows[-1]
            for seg in segments:
                seg.ordinal = len(previous.segments)
                previous.segments.append(seg)
            continue
        if iso is None:
            # Fall back to a date carried by the segment titles themselves.
            for seg in segments:
                _, seg_iso, _ = split_title(seg.title)
                if seg_iso:
                    iso, confidence = seg_iso, "inferred"
                    weekday = WEEKDAYS[date.fromisoformat(iso).weekday()]
                    break
        shows.append(Show(len(shows), iso, weekday, header, confidence, segments))

    toc_total = sum(len(v) for v in toc.values())
    # In the recap era the day sections carry roughly as many segments as the
    # table of contents. From late 2023 they hold only a standing note, so the
    # table of contents is the real content.
    if toc_total and prose_segments < 0.6 * toc_total:
        page_format = "listing"
        shows = _shows_from_toc(toc, week_start)
    elif prose_segments or any(s.segments for s in shows):
        page_format = "recap"
        _enrich_air_times(shows, toc)
    else:
        flat = parse_flat(html)
        if flat:
            page_format = "recap"
            shows = flat
        else:
            page_format = "unknown"

    return ParsedPage(url, page_format, week_start, week_end, shows)


def _shows_from_toc(toc, week_start: str | None) -> list[Show]:
    """Build shows from the table of contents (listing-format pages)."""
    order = {abbr: i for i, abbr in enumerate(
        ["mon", "tue", "wed", "thu", "fri", "sat", "sun"])}
    shows = []
    for day in sorted(toc, key=lambda d: order.get(d, 9)):
        items = toc[day]
        iso = next((d for _, d, _ in items if d), None)
        confidence = "exact"
        if iso is None and week_start:
            iso = date.fromordinal(
                date.fromisoformat(week_start).toordinal() + order.get(day, 0)
            ).isoformat()
            confidence = "inferred"
        segments = [
            Segment(title=title, ordinal=i, air_time=air_time)
            for i, (title, _, air_time) in enumerate(items)
        ]
        weekday = WEEKDAYS[date.fromisoformat(iso).weekday()] if iso else None
        shows.append(Show(len(shows), iso, weekday, day,
                          confidence if iso else "unknown", segments))
    return shows


def _enrich_air_times(shows: list[Show], toc) -> None:
    """Copy air times from the table of contents onto matching prose segments."""
    lookup = {}
    for items in toc.values():
        for title, _, air_time in items:
            if air_time:
                lookup.setdefault(_norm(title), air_time)
    if not lookup:
        return
    for show in shows:
        for seg in show.segments:
            if seg.air_time is None:
                seg.air_time = lookup.get(_norm(seg.title))


def _norm(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", title.lower())
