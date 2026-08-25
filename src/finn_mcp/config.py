from __future__ import annotations

import os
from pathlib import Path

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
)

LISTING_TTL_SECONDS = 24 * 60 * 60

def _env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.environ.get(name, default)))
    except ValueError:
        return default


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(os.environ.get(name, default)))
    except ValueError:
        return default


REQUEST_DELAY_MIN = _env_float("FINN_REQUEST_DELAY_MIN", 0.3)
REQUEST_DELAY_MAX = max(
    REQUEST_DELAY_MIN, _env_float("FINN_REQUEST_DELAY_MAX", 1.2)
)
REQUESTS_PER_MINUTE = _env_int("FINN_REQUESTS_PER_MINUTE", 20, 1)
HTTP_MAX_RETRIES = _env_int("FINN_HTTP_MAX_RETRIES", 3, 1)
HTTP_TIMEOUT_SECONDS = _env_float("FINN_HTTP_TIMEOUT_SECONDS", 15.0, 1.0)
MAX_QUERY_LENGTH = _env_int("FINN_MAX_QUERY_LENGTH", 200, 1)
MAX_FILTERS = _env_int("FINN_MAX_FILTERS", 20, 0)
MAX_SAVED_FINNKODES = _env_int("FINN_MAX_SAVED_FINNKODES", 1000, 1)
MAX_CACHE_BYTES = _env_int("FINN_MAX_CACHE_BYTES", 250 * 1024 * 1024, 0)
CACHE_CLEANUP_INTERVAL_SECONDS = _env_int(
    "FINN_CACHE_CLEANUP_INTERVAL_SECONDS", 24 * 60 * 60, 60
)
CACHE_RAW_HTML = os.environ.get("FINN_CACHE_RAW_HTML", "").lower() in {
    "1", "true", "yes", "on"
}

# Search pages ship their results as base64 JSON alongside the markup; see
# scraper/dehydrated.py. Off by one env var, because a change at finn.no
# should be recoverable without a new image.
USE_DEHYDRATED_STATE = os.environ.get(
    "FINN_USE_DEHYDRATED_STATE", "1"
).lower() in {"1", "true", "yes", "on"}
# Raise instead of quietly falling back to card scraping. For CI, where a
# silent fallback would let a finn.no change land unnoticed.
DEHYDRATED_STATE_STRICT = os.environ.get(
    "FINN_DEHYDRATED_STATE_STRICT", ""
).lower() in {"1", "true", "yes", "on"}
# Decoding guards: the real blob is ~400 KB encoded, so these are ceilings,
# not targets.
MAX_STATE_B64_CHARS = _env_int("FINN_MAX_STATE_B64_CHARS", 12 * 1024 * 1024, 1024)
MAX_STATE_BYTES = _env_int("FINN_MAX_STATE_BYTES", 8 * 1024 * 1024, 1024)


def data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    path = Path(base) / "finn-mcp"
    path.mkdir(parents=True, exist_ok=True)
    return path


def cache_db_path() -> Path:
    override = os.environ.get("FINN_CACHE_DB")
    if override:
        return Path(override)
    return data_dir() / "cache.sqlite"


def backend_name() -> str:
    return os.environ.get("FINN_BACKEND", "scraper").strip().lower()


def get_backend():
    """Return a fresh FinnBackend instance selected by the FINN_BACKEND env var.

    Deferred import so the scraper dependencies aren't loaded when the
    official backend is active (and vice-versa).
    """
    name = backend_name()
    if name == "scraper":
        from .backend.scraper_backend import ScraperBackend

        return ScraperBackend()
    if name == "official":
        from .backend.api_backend import OfficialApiBackend

        return OfficialApiBackend(api_key=os.environ.get("FINN_API_KEY"))
    raise ValueError(
        f"unknown FINN_BACKEND={name!r}; expected 'scraper' or 'official'"
    )
