from __future__ import annotations

import base64
import json

import pytest

from finn_mcp.scraper import dehydrated
from .conftest import read_fixture


def _wrap(payload: dict) -> str:
    """Encode a payload the way finn.no ships one.

    Padded past the minimum blob length on purpose: without it these fixtures
    would be skipped as noise and the tests below would pass for the wrong
    reason.
    """
    padded = {**payload, "_pad": "x" * 3000}
    blob = base64.b64encode(json.dumps(padded).encode()).decode()
    return f"<html><body><script>{blob}</script></body></html>"


# ---------- real pages ----------

@pytest.mark.parametrize(
    "fixture,prefix,expected_key",
    [
        ("jobs_search.html", "SEARCH_ID_JOB_", "SEARCH_ID_JOB_FULLTIME"),
        ("bap_search.html", "SEARCH_ID_BAP_", "SEARCH_ID_BAP_COMMON"),
        ("cars_used_search.html", "SEARCH_ID_CAR_USED", "SEARCH_ID_CAR_USED"),
    ],
)
def test_finds_payload_on_search_pages(fixture, prefix, expected_key):
    payload = dehydrated.find_search_payload(read_fixture(fixture), prefix)
    assert payload is not None
    assert payload.search_key == expected_key
    assert payload.docs and all(isinstance(d, dict) for d in payload.docs)
    assert payload.filters


@pytest.mark.parametrize("fixture", ["homes_search.html", "lettings_search.html"])
def test_returns_none_where_finn_does_not_ship_state(fixture):
    """Real estate runs on an older app with no embedded state.

    Callers must keep scraping cards for these, so None has to be a normal
    answer rather than an error.
    """
    assert dehydrated.find_search_payload(read_fixture(fixture), "SEARCH_ID_") is None


@pytest.mark.parametrize(
    "fixture",
    ["jobs_ad.html", "jobs_ad_no_jsonld.html", "bap_item.html", "homes_ad.html"],
)
def test_returns_none_on_detail_pages(fixture):
    assert dehydrated.find_search_payload(read_fixture(fixture), None) is None
    assert dehydrated.find_search_payload(read_fixture(fixture), "SEARCH_ID_") is None


def test_does_not_return_paid_placements_from_car_search():
    """Car searches carry a `poleposition` block holding paid placements.

    It sits in the same envelope as the real results, so picking a block by
    size or position would let sponsored ads through as search hits. It has
    `results` where a search payload has `docs`, which is why selection
    requires both `docs` and `metadata`.
    """
    html = read_fixture("cars_used_search.html")
    payload = dehydrated.find_search_payload(html, "SEARCH_ID_CAR_USED")
    assert payload is not None
    assert "poleposition" not in json.dumps(payload.metadata)
    assert all("docs" not in d for d in payload.docs)


def test_reports_the_real_match_count_not_the_page_size():
    payload = dehydrated.find_search_payload(
        read_fixture("jobs_search.html"), "SEARCH_ID_JOB_"
    )
    assert payload is not None
    assert payload.match_count == 7237
    assert len(payload.docs) == 50
    assert payload.last_page == 50


# ---------- selection and guards ----------

def test_no_prefix_means_no_payload():
    """A vertical that has not declared a search key must not match anything."""
    html = read_fixture("jobs_search.html")
    assert dehydrated.find_search_payload(html, None) is None


def test_prefix_must_match_the_vertical():
    html = read_fixture("jobs_search.html")
    assert dehydrated.find_search_payload(html, "SEARCH_ID_CAR_") is None


def test_ignores_blocks_without_docs_or_metadata():
    html = _wrap({"queries": [
        {"queryKey": [{"scope": "sort"}], "state": {"data": [1, 2, 3]}},
        {"queryKey": [{"scope": "seo"}], "state": {"data": {"seoData": {}}}},
        {"state": {"data": {"results": [{"id": "1"}], "metadata": {
            "search_key": "SEARCH_ID_BAP_COMMON"}}}},
    ]})
    assert dehydrated.find_search_payload(html, "SEARCH_ID_") is None


def test_survives_blobs_that_are_not_base64_json():
    html = (
        "<script>" + "!" * 3000 + "</script>"
        "<script>" + "A" * 3001 + "</script>"
        "<script>" + base64.b64encode(b"[1, 2, 3]" + b" " * 3000).decode() + "</script>"
        "<script>" + base64.b64encode(b'{"queries": [' + b"x" * 3000).decode() + "</script>"
    )
    assert dehydrated.find_search_payload(html, "SEARCH_ID_") is None


def test_oversized_payload_is_refused(monkeypatch):
    html = _wrap({"queries": [{"state": {"data": {
        "docs": [{"id": "1"}],
        "metadata": {"search_key": "SEARCH_ID_BAP_COMMON"},
    }}}]})
    assert dehydrated.find_search_payload(html, "SEARCH_ID_") is not None
    monkeypatch.setattr("finn_mcp.config.MAX_STATE_BYTES", 16)
    assert dehydrated.find_search_payload(html, "SEARCH_ID_") is None


def test_non_dict_docs_are_dropped_rather_than_failing_the_page():
    html = _wrap({"queries": [{"state": {"data": {
        "docs": ["junk", {"id": "1", "heading": "real"}, None],
        "metadata": {"search_key": "SEARCH_ID_BAP_COMMON"},
    }}}]})
    payload = dehydrated.find_search_payload(html, "SEARCH_ID_")
    assert payload is not None
    assert payload.docs == [{"id": "1", "heading": "real"}]
