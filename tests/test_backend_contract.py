"""Contract tests shared across all FinnBackend implementations.

The scraper backend is exercised with local HTML fixtures served by a
stub HTTP fetch. The official API backend is skipped until it's wired up.
"""

from __future__ import annotations

import pytest

from finn_mcp import http_client
from finn_mcp.backend.api_backend import OfficialApiBackend
from finn_mcp.backend.scraper_backend import ScraperBackend
from finn_mcp.backend.scraper_backend import parse_listing_reference
from finn_mcp.cache import Cache
from .conftest import read_fixture

SCRAPER_FIXTURE_MAP = {
    "https://www.finn.no/recommerce/forsale/search": "bap_search.html",
    "https://www.finn.no/recommerce/forsale/item/460211124": "bap_item.html",
    "https://www.finn.no/realestate/homes/search.html": "homes_search.html",
    "https://www.finn.no/realestate/homes/ad.html": "homes_ad.html",
    "https://www.finn.no/mobility/item/460212366": "mobility_item.html",
    "https://www.finn.no/job/search": "jobs_search.html",
    "https://www.finn.no/job/ad/459777164": "jobs_ad.html",
}


@pytest.fixture
def stub_http(monkeypatch):
    async def fake_fetch(url, params=None, polite=True):
        # Normalise ?finnkode=... into the base URL so the homes detail matches.
        for key, fixture in SCRAPER_FIXTURE_MAP.items():
            if url.startswith(key):
                return read_fixture(fixture)
        raise http_client.NotFoundError(url)

    monkeypatch.setattr(http_client, "fetch", fake_fetch)
    return fake_fetch


@pytest.fixture
def backends(tmp_path, stub_http):
    return [
        ScraperBackend(cache=Cache(db_path=tmp_path / "c.sqlite")),
        # OfficialApiBackend is a stub — included for future coverage,
        # currently expected to raise NotImplementedError in every test.
    ]


async def test_backend_search_bap(backends):
    for backend in backends:
        response = await backend.search("bap", query="iphone")
        assert response.results, f"{backend.name}: empty search"
        assert response.results[0].vertical == "bap"
        assert response.results[0].finnkode.isdigit()
        assert response.search_url.startswith("https://www.finn.no/")
        assert response.page == 1


async def test_backend_get_listing_bap(backends):
    for backend in backends:
        listing = await backend.get_listing("460211124", vertical="bap")
        assert listing.vertical == "bap"
        assert listing.title
        assert listing.price is not None


async def test_backend_get_listing_cached(backends):
    for backend in backends:
        first = await backend.get_listing("460211124", vertical="bap")
        # Second call should come from cache — same object shape.
        second = await backend.get_listing("460211124", vertical="bap")
        assert first.finnkode == second.finnkode
        assert first.title == second.title


async def test_backend_get_listing_probes_vertical_when_unknown(backends):
    for backend in backends:
        # 460211124 is a BAP item; vertical should be inferred.
        listing = await backend.get_listing("460211124", vertical=None)
        assert listing.vertical == "bap"


async def test_backend_get_listing_infers_vertical_from_url(backends):
    for backend in backends:
        listing = await backend.get_listing(
            "https://www.finn.no/recommerce/forsale/item/460211124"
        )
        assert listing.vertical == "bap"


def test_parse_listing_reference_rejects_external_urls():
    with pytest.raises(ValueError):
        parse_listing_reference("https://example.com/item/123")


async def test_official_backend_raises_until_implemented():
    backend = OfficialApiBackend()
    with pytest.raises(NotImplementedError):
        await backend.search("bap", query="anything")
    with pytest.raises(NotImplementedError):
        await backend.get_listing("123", vertical="bap")


# ---------- embedded search state, and the fallback under it ----------

async def test_search_uses_embedded_state_when_finn_ships_it(backends):
    """Location is the tell.

    Torget cards have no stable DOM hook for it, so the card parser has
    always returned None there. A location coming back means the result was
    built from finn.no's own search payload rather than scraped off the page.
    """
    for backend in backends:
        response = await backend.search("bap", query="iphone")
        assert response.source == "state"
        assert response.results[0].location
        assert response.results[0].url.startswith("https://www.finn.no/")
        # The whole result set, not this page: 53 ads shown of 13723 matching.
        assert response.total_matches == 13723
        assert response.total_matches > len(response.results)
        assert response.last_page == 50


async def test_search_falls_back_to_cards_when_state_is_absent(backends):
    """Real estate search pages carry no embedded state at all."""
    for backend in backends:
        response = await backend.search("homes", query="oslo")
        assert response.source == "cards"
        assert response.results, "homes must keep working via card scraping"
        assert response.results[0].vertical == "homes"
        # Card scraping cannot see the full result set, so it must not claim to.
        assert response.total_matches is None
        assert response.last_page is None


async def test_search_still_works_with_the_state_layer_switched_off(
    backends, monkeypatch
):
    """FINN_USE_DEHYDRATED_STATE=0 has to be a usable escape hatch."""
    monkeypatch.setattr("finn_mcp.config.USE_DEHYDRATED_STATE", False)
    for backend in backends:
        response = await backend.search("bap", query="iphone")
        assert response.source == "cards"
        assert response.results
        assert response.results[0].finnkode.isdigit()


async def test_strict_mode_raises_instead_of_degrading_quietly(backends, monkeypatch):
    """CI should fail loudly if finn.no stops shipping the payload.

    Without this the layer would silently drop back to card scraping and the
    change would only surface as gradually worse results.
    """
    from finn_mcp.scraper.base import MissingSearchStateError

    monkeypatch.setattr("finn_mcp.config.DEHYDRATED_STATE_STRICT", True)
    monkeypatch.setattr("finn_mcp.scraper.bap.BapScraper.search_key_prefix", "SEARCH_ID_NOPE_")
    for backend in backends:
        with pytest.raises(MissingSearchStateError):
            await backend.search("bap", query="iphone")


async def test_extraction_counts_show_which_path_answered(backends):
    """A quiet fallback to card scraping has to be visible somewhere.

    Counted as deltas rather than absolutes because the counters are
    process-wide and other tests search too.
    """
    from finn_mcp.scraper.base import extraction_counts

    def count(vertical: str, source: str) -> int:
        return extraction_counts().get(vertical, {}).get(source, 0)

    before_state = count("bap", "state")
    before_homes = count("homes", "cards")
    before_bap_cards = count("bap", "cards")

    for backend in backends:
        await backend.search("bap", query="iphone")
        await backend.search("homes", query="oslo")

    assert count("bap", "state") == before_state + len(backends)
    assert count("homes", "cards") == before_homes + len(backends)
    # Nothing here should have pushed bap onto the card path.
    assert count("bap", "cards") == before_bap_cards


# ---------- filter discovery ----------

async def test_discover_filters_lists_what_can_be_filtered(backends):
    for backend in backends:
        summary = await backend.discover_filters("jobs", query="developer")
        names = {f["name"] for f in summary["filters"]}
        # Things a caller could never guess the parameter name for.
        assert {"location", "occupation", "home_office", "working_language"} <= names
        assert "q" not in names, "the query is not a filter to choose"
        assert summary["total_matches"] == 7237


async def test_discover_filters_expands_one_filter_with_hit_counts(backends):
    for backend in backends:
        summary = await backend.discover_filters(
            "jobs", query="developer", filter_name="location", max_items=5
        )
        assert summary["name"] == "location"
        options = summary["options"]
        assert len(options) <= 5
        assert options[0]["value"] and options[0]["label"]
        assert options[0]["hits"] > 0
        # Sorted by hits, so the useful branches come first.
        hits = [o["hits"] for o in options if "hits" in o]
        assert hits == sorted(hits, reverse=True)


async def test_discover_filters_drills_into_a_named_option(backends):
    """Location is country, county, municipality.

    Expanding the filter shows Norge and Utlandet, which narrows nothing on
    its own -- the counties are a level down, and there has to be a way in.
    """
    for backend in backends:
        top = await backend.discover_filters(
            "jobs", query="developer", filter_name="location"
        )
        norway = next(o for o in top["options"] if o["label"] == "Norge")
        assert norway["narrows_further"] == 16

        counties = await backend.discover_filters(
            "jobs", query="developer", filter_name="location", value=norway["value"]
        )
        assert counties["within"] == ["Norge"]
        assert len(counties["options"]) == 16
        assert any(o["label"] == "Agder" for o in counties["options"])


async def test_discover_filters_says_when_it_truncated(backends):
    for backend in backends:
        summary = await backend.discover_filters(
            "jobs", query="developer", filter_name="location",
            value="0.20001", max_items=3,
        )
        assert len(summary["options"]) == 3
        assert summary["not_shown"] == 13, "silent truncation reads as completeness"


async def test_discover_filters_rejects_an_unknown_value(backends):
    for backend in backends:
        summary = await backend.discover_filters(
            "jobs", query="developer", filter_name="location", value="9.99999"
        )
        assert summary["error"] == "unknown_value"


async def test_discover_filters_is_honest_about_real_estate(backends):
    """No embedded state means no filter data. Say so rather than return {}."""
    for backend in backends:
        summary = await backend.discover_filters("homes", query="oslo")
        assert summary["error"] == "not_supported"


async def test_discover_filters_rejects_an_unknown_filter(backends):
    for backend in backends:
        summary = await backend.discover_filters(
            "jobs", query="developer", filter_name="nonsense"
        )
        assert summary["error"] == "unknown_filter"
