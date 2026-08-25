from __future__ import annotations

import asyncio
import random
import time
from collections import deque
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from . import config


class RateLimitedError(Exception):
    def __init__(self, retry_after: int = 60):
        super().__init__(f"finn.no rate-limited (retry after {retry_after}s)")
        self.retry_after = retry_after


class NotFoundError(Exception):
    pass


_client: httpx.AsyncClient | None = None
_semaphore: asyncio.Semaphore | None = None
_request_times: deque[float] = deque()


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(1)
    return _semaphore


async def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            http2=True,
            timeout=config.HTTP_TIMEOUT_SECONDS,
            headers={
                "User-Agent": config.USER_AGENT,
                "Accept-Language": "nb-NO,nb;q=0.9,en;q=0.7",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
            follow_redirects=True,
        )
    return _client


async def close_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


async def fetch(url: str, *, params: dict[str, Any] | None = None, polite: bool = True) -> str:
    client = await get_client()
    sem = _get_semaphore()
    async with sem:
        if polite:
            await asyncio.sleep(
                random.uniform(config.REQUEST_DELAY_MIN, config.REQUEST_DELAY_MAX)
            )
        now = time.monotonic()
        while _request_times and now - _request_times[0] >= 60:
            _request_times.popleft()
        if len(_request_times) >= config.REQUESTS_PER_MINUTE:
            await asyncio.sleep(max(0.0, 60 - (now - _request_times[0])))
        _request_times.append(time.monotonic())

        for attempt in range(1, config.HTTP_MAX_RETRIES + 1):
            try:
                response = await client.get(url, params=params)
            except (httpx.TimeoutException, httpx.RemoteProtocolError, httpx.NetworkError):
                if attempt == config.HTTP_MAX_RETRIES:
                    raise
                await asyncio.sleep(2 ** (attempt - 1) + random.uniform(0, 1.0))
                continue

            if response.status_code == 404:
                raise NotFoundError(url)
            if response.status_code == 429:
                retry_after = _retry_after_seconds(response.headers.get("Retry-After"))
                if attempt < config.HTTP_MAX_RETRIES:
                    await asyncio.sleep(retry_after)
                    continue
                raise RateLimitedError(retry_after=retry_after)
            if 500 <= response.status_code < 600 and attempt < config.HTTP_MAX_RETRIES:
                await asyncio.sleep(2 ** (attempt - 1) + random.uniform(0, 1.0))
                continue
            response.raise_for_status()
            return response.text

        raise RuntimeError(f"unreachable: exhausted retries for {url}")


def _retry_after_seconds(value: str | None) -> int:
    if not value:
        return 30
    try:
        return max(1, min(300, int(value)))
    except ValueError:
        try:
            delay = parsedate_to_datetime(value).timestamp() - time.time()
            return max(1, min(300, int(delay)))
        except (TypeError, ValueError, OverflowError):
            return 30
