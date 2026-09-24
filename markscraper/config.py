"""Shared constants and paths."""
from pathlib import Path

BASE_URL = "https://www.marksfriggin.com"

# The two archive index pages that list every weekly page.
INDEX_PAGES = [
    f"{BASE_URL}/old96-05.htm",   # 1986-2005
    f"{BASE_URL}/old-news.htm",   # 2006-present
]

# The live page, which carries the current in-progress week before it is archived.
CURRENT_PAGE = f"{BASE_URL}/news.htm"

# The site returns HTTP 406 for short User-Agent strings. A full browser UA works.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Pages are cp1252, and the server sends no reliable charset.
SITE_ENCODING = "cp1252"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
CACHE_DIR = DATA_DIR / "cache"
DB_PATH = DATA_DIR / "marksfriggin.db"

DEFAULT_RATE = 1.0        # seconds between requests
REQUEST_TIMEOUT = 30.0
MAX_RETRIES = 4

# Standard list price ($/1M tokens) for input/output; batch runs at half.
MODEL_PRICING = {
    "claude-opus-5":    (5.00, 25.00),
    "claude-sonnet-5":  (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}
