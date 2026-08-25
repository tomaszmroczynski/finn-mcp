from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

from finn_mcp.http_client import _retry_after_seconds


def test_retry_after_seconds():
    assert _retry_after_seconds("12") == 12
    assert _retry_after_seconds(None) == 30
    future = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=10))
    assert 1 <= _retry_after_seconds(future) <= 10
