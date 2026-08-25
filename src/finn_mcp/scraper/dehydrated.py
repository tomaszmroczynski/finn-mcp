"""Read the search payload finn.no ships alongside the rendered page.

finn.no's search pages are a client-side app that is server-rendered: the
response carries both the markup and the data that markup was built from, so
the browser can take over without asking for it again. The data sits in a
<script> as base64-encoded JSON -- a dehydrated TanStack Query cache.

Reading that instead of the markup gives the fields finn.no's own frontend
uses (make, model, mileage, company_name, deadline, the real match count)
without a second request, and does not care how the page is styled.

It is not available everywhere. Homes and lettings search pages do not carry
it, and no detail page does, so callers must keep their existing path as a
fallback rather than replacing it.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from typing import Any, Iterator

from selectolax.parser import HTMLParser

from .. import config

# Base64 alphabet only; anything with markup, quotes or whitespace is not a blob.
_B64_RE = re.compile(r"^[A-Za-z0-9+/=]+$")

# Short blobs are page config, not search results. Selection is by shape, not
# by size -- this only avoids decoding obvious noise.
_MIN_BLOB_CHARS = 2000


@dataclass(frozen=True)
class SearchPayload:
    """One search response as finn.no's own frontend received it."""

    docs: list[dict[str, Any]]
    filters: list[dict[str, Any]]
    metadata: dict[str, Any]
    search_key: str

    @property
    def match_count(self) -> int | None:
        """Total ads matching the query, not the number on this page."""
        size = self.metadata.get("result_size")
        if isinstance(size, dict):
            count = size.get("match_count")
            if isinstance(count, int):
                return count
        return None

    @property
    def last_page(self) -> int | None:
        paging = self.metadata.get("paging")
        if isinstance(paging, dict):
            last = paging.get("last")
            if isinstance(last, int):
                return last
        return None


def _decode_blobs(html: str) -> Iterator[dict[str, Any]]:
    """Yield every <script> whose body is base64 decoding to a JSON object."""
    for node in HTMLParser(html).css("script"):
        raw = (node.text(deep=True) or "").strip()
        if len(raw) < _MIN_BLOB_CHARS or len(raw) > config.MAX_STATE_B64_CHARS:
            continue
        if not _B64_RE.fullmatch(raw):
            continue
        try:
            decoded = base64.b64decode(raw + "=" * (-len(raw) % 4), validate=True)
        except (binascii.Error, ValueError):
            continue
        if len(decoded) > config.MAX_STATE_BYTES:
            continue
        try:
            obj = json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(obj, dict):
            yield obj


def find_search_payload(html: str, search_key_prefix: str | None) -> SearchPayload | None:
    """Return the search payload for this vertical, or None if the page has none.

    ``search_key_prefix`` is matched against ``metadata.search_key`` as a
    prefix, because a vertical can have several (jobs alone has fulltime,
    part-time and management keys). Passing None accepts any key.

    Selection is by shape. A search page carries several query entries, and at
    least one of them is not what we want: car searches include a
    ``poleposition`` block holding paid placements, which has ``results``
    rather than ``docs``. Requiring both ``docs`` and ``metadata`` keeps
    sponsored ads out of search results.
    """
    if search_key_prefix is None:
        return None
    for obj in _decode_blobs(html):
        queries = obj.get("queries")
        if not isinstance(queries, list):
            continue
        for query in queries:
            if not isinstance(query, dict):
                continue
            data = (query.get("state") or {}).get("data")
            if not isinstance(data, dict):
                continue
            docs = data.get("docs")
            metadata = data.get("metadata")
            if not isinstance(docs, list) or not docs:
                continue
            if not isinstance(metadata, dict):
                continue
            key = metadata.get("search_key")
            if not isinstance(key, str) or not key.startswith(search_key_prefix):
                continue
            filters = data.get("filters")
            return SearchPayload(
                docs=[d for d in docs if isinstance(d, dict)],
                filters=filters if isinstance(filters, list) else [],
                metadata=metadata,
                search_key=key,
            )
    return None
