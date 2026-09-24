"""Polite, cached HTTP for marksfriggin.com."""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import requests

from . import config


@dataclass
class Fetched:
    url: str
    text: str
    status: int
    from_cache: bool
    etag: str | None = None
    last_modified: str | None = None

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8", "replace")).hexdigest()

    @property
    def byte_size(self) -> int:
        return len(self.text.encode("utf-8", "replace"))


def cache_path(url: str) -> Path:
    """Mirror the site's own path layout under data/cache."""
    parts = [p for p in urlparse(url).path.split("/") if p]
    if not parts:
        parts = ["index.htm"]
    return config.CACHE_DIR.joinpath(*parts)


def read_cache(url: str) -> str | None:
    """Return the cached copy of a page, or None if it was never fetched."""
    path = cache_path(url)
    return path.read_text(encoding="utf-8") if path.exists() else None


class Fetcher:
    def __init__(self, rate: float = config.DEFAULT_RATE, refresh: bool = False):
        self.rate = rate
        self.refresh = refresh
        self._last_request = 0.0
        self.session = requests.Session()
        self.session.headers["User-Agent"] = config.USER_AGENT

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request
        if elapsed < self.rate:
            time.sleep(self.rate - elapsed)
        self._last_request = time.monotonic()

    def get(self, url: str, etag: str | None = None,
            last_modified: str | None = None, revalidate: bool = False) -> Fetched:
        """Fetch a URL, preferring the on-disk cache.

        With revalidate=True the cache is checked against the server with a
        conditional request instead of being trusted outright. The site sends no
        ETag, so Last-Modified is the only validator available -- it does answer
        If-Modified-Since with a 304.
        """
        path = cache_path(url)

        if path.exists() and not (self.refresh or revalidate):
            return Fetched(url, path.read_text(encoding="utf-8"), 200, from_cache=True)

        headers = {}
        # Only send conditional headers when we already hold a cached copy to fall back on.
        if path.exists() and last_modified:
            headers["If-Modified-Since"] = last_modified

        last_error: Exception | None = None
        for attempt in range(config.MAX_RETRIES):
            self._throttle()
            try:
                resp = self.session.get(
                    url, headers=headers, timeout=config.REQUEST_TIMEOUT
                )
            except requests.RequestException as exc:
                last_error = exc
                time.sleep(min(2**attempt, 30))
                continue

            if resp.status_code == 304 and path.exists():
                return Fetched(url, path.read_text(encoding="utf-8"), 304,
                               from_cache=True, etag=etag, last_modified=last_modified)

            if resp.status_code >= 500 or resp.status_code == 429:
                last_error = RuntimeError(f"HTTP {resp.status_code} for {url}")
                time.sleep(min(2**attempt, 30))
                continue

            # The server sends no reliable charset; these pages are cp1252.
            text = resp.content.decode(config.SITE_ENCODING, errors="replace")

            if resp.status_code == 200:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")

            return Fetched(
                url, text, resp.status_code, from_cache=False,
                etag=resp.headers.get("ETag"),
                last_modified=resp.headers.get("Last-Modified"),
            )

        raise RuntimeError(f"failed to fetch {url}: {last_error}")
