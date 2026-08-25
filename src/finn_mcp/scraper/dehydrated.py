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


# Not a filter the caller can choose -- it is the query they already typed.
_NON_FILTER_TYPES = {"QUERY_FILTER"}


def _option(item: dict[str, Any]) -> dict[str, Any]:
    children = item.get("filter_items")
    out: dict[str, Any] = {
        "value": item.get("value"),
        "label": item.get("display_name"),
    }
    hits = item.get("hits")
    # finn.no uses -1 for "not counted" rather than omitting the field.
    if isinstance(hits, int) and hits >= 0:
        out["hits"] = hits
    if item.get("selected"):
        out["selected"] = True
    if isinstance(children, list) and children:
        out["narrows_further"] = len(children)
    return out


def _find_by_value(items: list[Any], value: str) -> dict[str, Any] | None:
    for item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("value")) == value:
            return item
        children = item.get("filter_items")
        if isinstance(children, list):
            found = _find_by_value(children, value)
            if found is not None:
                return found
    return None


def summarize_filters(
    payload: SearchPayload,
    filter_name: str | None = None,
    value: str | None = None,
    max_items: int = 25,
) -> dict[str, Any]:
    """Describe what can be filtered, in a form that fits in a reply.

    finn.no's filter tree is not something to hand over whole: the location
    branch alone is every municipality in Norway, three levels deep. So the
    overview lists filter names and how many options each has, and naming one
    expands just that filter, one level down, biggest first.

    Hit counts come along because they tell a caller which branches are worth
    taking -- and which would return nothing.
    """
    overview: list[dict[str, Any]] = []
    for entry in payload.filters:
        if not isinstance(entry, dict) or entry.get("type") in _NON_FILTER_TYPES:
            continue
        name = entry.get("name")
        items = entry.get("filter_items")
        if not isinstance(name, str) or not isinstance(items, list):
            continue

        if filter_name is None:
            overview.append(
                {"name": name, "label": entry.get("display_name"), "options": len(items)}
            )
            continue

        if name != filter_name:
            continue

        # A level offering a single option is not a choice. Location opens on
        # "Norge" alone with the counties underneath it, so expanding one
        # level would hand the caller a dead end. Descend until there is
        # something to pick between, and say what was skipped.
        path: list[str] = []
        items = [i for i in items if isinstance(i, dict)]

        # Drilling into a named option. Location is three levels deep --
        # country, county, municipality -- and a caller that can only see the
        # top of it cannot narrow anything down.
        if value is not None:
            node = _find_by_value(items, value)
            if node is None:
                return {"error": "unknown_value", "name": name, "value": value}
            children = node.get("filter_items")
            if not isinstance(children, list) or not children:
                return {
                    "name": name,
                    "label": entry.get("display_name"),
                    "within": [str(node.get("display_name"))],
                    "options": [],
                    "message": "this option narrows no further",
                }
            path.append(str(node.get("display_name")))
            items = [i for i in children if isinstance(i, dict)]
        while len(items) == 1 and isinstance(items[0].get("filter_items"), list) and items[0]["filter_items"]:
            path.append(str(items[0].get("display_name")))
            items = [i for i in items[0]["filter_items"] if isinstance(i, dict)]

        options = [_option(i) for i in items]
        # Selected branches are kept whatever the cut-off, so a caller never
        # loses sight of the filter it is already inside.
        chosen = [o for o in options if o.get("selected")]
        rest = sorted(
            (o for o in options if not o.get("selected")),
            key=lambda o: o.get("hits", 0),
            reverse=True,
        )
        shown = chosen + rest[: max(0, max_items - len(chosen))]
        result: dict[str, Any] = {
            "name": name,
            "label": entry.get("display_name"),
            "options": shown,
        }
        if path:
            result["within"] = path
        dropped = len(options) - len(shown)
        if dropped > 0:
            result["not_shown"] = dropped
        return result

    if filter_name is not None:
        return {"error": "unknown_filter", "name": filter_name}
    return {"filters": overview}
