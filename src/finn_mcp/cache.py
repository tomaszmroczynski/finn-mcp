from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config
from .models import Listing, SavedSearch, Vertical

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
  finnkode     TEXT PRIMARY KEY,
  vertical     TEXT NOT NULL,
  fetched_at   INTEGER NOT NULL,
  raw_html     TEXT,
  parsed_json  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_listings_vertical ON listings(vertical);

CREATE TABLE IF NOT EXISTS saved_searches (
  name                TEXT PRIMARY KEY,
  vertical            TEXT NOT NULL,
  query               TEXT NOT NULL,
  filters_json        TEXT NOT NULL DEFAULT '{}',
  last_checked_at     INTEGER,
  last_finnkodes_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


def _connect(db_path: Path | str) -> sqlite3.Connection:
    if isinstance(db_path, Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    if str(db_path) != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


class Cache:
    def __init__(self, db_path: Path | str | None = None):
        self.db_path: Path | str = db_path if db_path is not None else config.cache_db_path()
        self._cleanup_sync()
        self._last_cleanup = time.monotonic()

    def _conn(self) -> sqlite3.Connection:
        return _connect(self.db_path)

    # ---------- listings ----------

    def _get_listing(self, finnkode: str, max_age_seconds: int) -> dict[str, Any] | None:
        cutoff = int(time.time()) - max_age_seconds
        with self._conn() as c:
            row = c.execute(
                "SELECT vertical, fetched_at, raw_html, parsed_json "
                "FROM listings WHERE finnkode = ? AND fetched_at > ?",
                (finnkode, cutoff),
            ).fetchone()
        if row is None:
            return None
        return {
            "vertical": row["vertical"],
            "fetched_at": row["fetched_at"],
            "raw_html": row["raw_html"],
            "parsed_json": row["parsed_json"],
        }

    def _get_vertical_hint(self, finnkode: str) -> Vertical | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT vertical FROM listings WHERE finnkode = ?",
                (finnkode,),
            ).fetchone()
        return row["vertical"] if row else None

    def _put_listing(self, listing: Listing, raw_html: str | None) -> None:
        payload = listing.model_dump(mode="json")
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO listings "
                "(finnkode, vertical, fetched_at, raw_html, parsed_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    listing.finnkode,
                    listing.vertical,
                    int(listing.fetched_at.timestamp()),
                    raw_html if config.CACHE_RAW_HTML else "",
                    json.dumps(payload),
                ),
            )
            c.commit()
        if (
            time.monotonic() - self._last_cleanup
            >= config.CACHE_CLEANUP_INTERVAL_SECONDS
        ):
            self._cleanup_sync()

    async def get_listing(
        self, finnkode: str, max_age_seconds: int = config.LISTING_TTL_SECONDS
    ) -> Listing | None:
        row = await asyncio.to_thread(self._get_listing, finnkode, max_age_seconds)
        if row is None:
            return None
        return Listing.model_validate_json(row["parsed_json"])

    async def vertical_hint(self, finnkode: str) -> Vertical | None:
        return await asyncio.to_thread(self._get_vertical_hint, finnkode)

    async def put_listing(self, listing: Listing, raw_html: str | None = None) -> None:
        await asyncio.to_thread(self._put_listing, listing, raw_html)

    def _cleanup_sync(self, *, vacuum: bool = False) -> dict[str, int]:
        cutoff = int(time.time()) - config.LISTING_TTL_SECONDS
        with self._conn() as c:
            removed = c.execute(
                "DELETE FROM listings WHERE fetched_at <= ?", (cutoff,)
            ).rowcount
            if config.MAX_CACHE_BYTES and self._db_size() > config.MAX_CACHE_BYTES:
                count = c.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
                to_remove = max(1, count // 4)
                removed += c.execute(
                    "DELETE FROM listings WHERE finnkode IN ("
                    "SELECT finnkode FROM listings ORDER BY fetched_at ASC LIMIT ?"
                    ")",
                    (to_remove,),
                ).rowcount
                vacuum = True
            c.commit()
            if vacuum:
                c.execute("VACUUM")
        self._last_cleanup = time.monotonic()
        return {"removed_listings": removed}

    def _db_size(self) -> int:
        if self.db_path == ":memory:":
            return 0
        path = Path(self.db_path)
        return path.stat().st_size if path.exists() else 0

    async def cleanup(self, *, vacuum: bool = False) -> dict[str, int]:
        return await asyncio.to_thread(self._cleanup_sync, vacuum=vacuum)

    def _clear_sync(self) -> dict[str, int]:
        with self._conn() as c:
            removed = c.execute("DELETE FROM listings").rowcount
            c.commit()
            c.execute("VACUUM")
        return {"removed_listings": removed}

    async def clear(self) -> dict[str, int]:
        return await asyncio.to_thread(self._clear_sync)

    def _status_sync(self) -> dict[str, Any]:
        with self._conn() as c:
            listings = c.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
            searches = c.execute("SELECT COUNT(*) FROM saved_searches").fetchone()[0]
        return {
            "listings": listings,
            "saved_searches": searches,
            "size_bytes": self._db_size(),
            "max_size_bytes": config.MAX_CACHE_BYTES,
            "raw_html_enabled": config.CACHE_RAW_HTML,
        }

    async def status(self) -> dict[str, Any]:
        return await asyncio.to_thread(self._status_sync)

    # ---------- saved searches ----------

    def _save_search(self, s: SavedSearch) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO saved_searches "
                "(name, vertical, query, filters_json, last_checked_at, last_finnkodes_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    s.name,
                    s.vertical,
                    s.query,
                    json.dumps(s.filters),
                    int(s.last_checked_at.timestamp()) if s.last_checked_at else None,
                    json.dumps(s.last_finnkodes),
                ),
            )
            c.commit()

    def _get_search(self, name: str) -> SavedSearch | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT name, vertical, query, filters_json, last_checked_at, last_finnkodes_json "
                "FROM saved_searches WHERE name = ?",
                (name,),
            ).fetchone()
        if row is None:
            return None
        return _row_to_saved_search(row)

    def _list_searches(self) -> list[SavedSearch]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT name, vertical, query, filters_json, last_checked_at, last_finnkodes_json "
                "FROM saved_searches ORDER BY name"
            ).fetchall()
        return [_row_to_saved_search(r) for r in rows]

    def _delete_search(self, name: str) -> bool:
        with self._conn() as c:
            cur = c.execute("DELETE FROM saved_searches WHERE name = ?", (name,))
            c.commit()
            return cur.rowcount > 0

    def _update_search_state(
        self, name: str, checked_at: datetime, finnkodes: list[str]
    ) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE saved_searches SET last_checked_at = ?, last_finnkodes_json = ? "
                "WHERE name = ?",
                (
                    int(checked_at.timestamp()),
                    json.dumps(finnkodes[-config.MAX_SAVED_FINNKODES :]),
                    name,
                ),
            )
            c.commit()

    async def save_search(self, s: SavedSearch) -> None:
        await asyncio.to_thread(self._save_search, s)

    async def get_search(self, name: str) -> SavedSearch | None:
        return await asyncio.to_thread(self._get_search, name)

    async def list_searches(self) -> list[SavedSearch]:
        return await asyncio.to_thread(self._list_searches)

    async def delete_search(self, name: str) -> bool:
        return await asyncio.to_thread(self._delete_search, name)

    async def update_search_state(
        self, name: str, checked_at: datetime, finnkodes: list[str]
    ) -> None:
        await asyncio.to_thread(self._update_search_state, name, checked_at, finnkodes)


def _row_to_saved_search(row: sqlite3.Row) -> SavedSearch:
    checked = row["last_checked_at"]
    return SavedSearch(
        name=row["name"],
        vertical=row["vertical"],
        query=row["query"],
        filters=json.loads(row["filters_json"] or "{}"),
        last_checked_at=(
            datetime.fromtimestamp(checked, tz=timezone.utc) if checked else None
        ),
        last_finnkodes=json.loads(row["last_finnkodes_json"] or "[]"),
    )
