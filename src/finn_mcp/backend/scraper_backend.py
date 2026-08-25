from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import parse_qs, urlparse

from .. import config, http_client
from ..cache import Cache
from ..models import ALL_VERTICALS, Listing, SearchResponse, Vertical
from ..scraper import get_scraper
from .base import FinnBackend, ListingNotFound

log = logging.getLogger(__name__)

# Probe order when the caller hasn't supplied a vertical. Roughly by popularity.
_VERTICAL_PROBE_ORDER: tuple[Vertical, ...] = (
    "cars_used",
    "bap",
    "homes",
    "lettings",
    "jobs",
)

_URL_VERTICALS: tuple[tuple[str, Vertical], ...] = (
    ("/recommerce/forsale/item/", "bap"),
    ("/mobility/item/", "cars_used"),
    ("/realestate/homes/", "homes"),
    ("/realestate/lettings/", "lettings"),
    ("/job/ad/", "jobs"),
)


def parse_listing_reference(value: str) -> tuple[str, Vertical | None]:
    """Return (finnkode, vertical hint) from a finnkode or a finn.no URL."""
    if value.isdigit():
        return value, None
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
        "finn.no", "www.finn.no"
    }:
        raise ValueError("listing reference must be a finnkode or finn.no URL")
    hint = next((v for marker, v in _URL_VERTICALS if marker in parsed.path), None)
    query_code = (parse_qs(parsed.query).get("finnkode") or [None])[0]
    path_match = re.search(r"/(?:item|ad)/(\d+)", parsed.path)
    finnkode = query_code or (path_match.group(1) if path_match else None)
    if not finnkode or not finnkode.isdigit():
        raise ValueError("could not find finnkode in URL")
    return finnkode, hint


class ScraperBackend(FinnBackend):
    name = "scraper"

    def __init__(self, cache: Cache | None = None):
        self.cache = cache or Cache()

    async def search(
        self,
        vertical: Vertical,
        query: str,
        page: int = 1,
        filters: dict[str, str] | None = None,
    ) -> SearchResponse:
        if vertical not in ALL_VERTICALS:
            raise ValueError(f"unknown vertical: {vertical}")
        if len(query) > config.MAX_QUERY_LENGTH:
            raise ValueError(f"query exceeds {config.MAX_QUERY_LENGTH} characters")
        if page < 1 or page > 100:
            raise ValueError("page must be between 1 and 100")
        if filters and len(filters) > config.MAX_FILTERS:
            raise ValueError(f"at most {config.MAX_FILTERS} filters are allowed")
        if filters and any(
            not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", key)
            or len(str(value)) > 500
            for key, value in filters.items()
        ):
            raise ValueError("invalid filter key or value")
        scraper = get_scraper(vertical)
        return await scraper.search(query=query, page=page, filters=filters)

    async def discover_filters(
        self,
        vertical: Vertical,
        query: str = "",
        filters: dict[str, str] | None = None,
        filter_name: str | None = None,
        value: str | None = None,
        max_items: int = 25,
    ) -> dict[str, Any]:
        if vertical not in ALL_VERTICALS:
            raise ValueError(f"unknown vertical: {vertical}")
        if len(query) > config.MAX_QUERY_LENGTH:
            raise ValueError(f"query exceeds {config.MAX_QUERY_LENGTH} characters")
        return await get_scraper(vertical).discover_filters(
            query, filters=filters, filter_name=filter_name, value=value,
            max_items=max(1, min(100, max_items)),
        )

    async def get_listing(
        self,
        finnkode: str,
        vertical: Vertical | None = None,
    ) -> Listing:
        finnkode, url_vertical = parse_listing_reference(finnkode.strip())
        if vertical is None:
            vertical = url_vertical

        if vertical is None:
            vertical = await self.cache.vertical_hint(finnkode)

        cached = await self.cache.get_listing(finnkode)
        if cached is not None and (vertical is None or cached.vertical == vertical):
            return cached

        if vertical is not None:
            return await self._fetch_and_cache(finnkode, vertical)

        # No hint, no cache — probing is retained as a backwards-compatible
        # fallback. Passing a result URL or vertical avoids these requests.
        last_exc: Exception | None = None
        for candidate in _VERTICAL_PROBE_ORDER:
            try:
                return await self._fetch_and_cache(finnkode, candidate)
            except http_client.NotFoundError:
                continue
            except Exception as exc:  # network, parse, etc.
                log.warning("probe %s failed for %s: %s", candidate, finnkode, exc)
                last_exc = exc
                continue
        if last_exc is not None:
            raise last_exc
        raise ListingNotFound(finnkode)

    async def _fetch_and_cache(self, finnkode: str, vertical: Vertical) -> Listing:
        scraper = get_scraper(vertical)
        try:
            html, listing = await scraper.fetch_detail(finnkode)
        except http_client.NotFoundError:
            raise
        await self.cache.put_listing(listing, html)
        return listing

    async def aclose(self) -> None:
        await http_client.close_client()
