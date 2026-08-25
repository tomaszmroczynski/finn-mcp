# finn-mcp

> **This is a fork of [aHk-coder/finn-mcp](https://github.com/aHk-coder/finn-mcp).**
> The server, its architecture and all four verticals are that project's work,
> by [aHk-coder](https://github.com/aHk-coder), under the MIT licence. This fork
> adapts it for unattended operation and fixes a parser that finn.no broke.
> It is not a rewrite and not an original project.

MCP server that exposes [finn.no](https://www.finn.no) — Norway's largest
online classifieds marketplace — to Claude across four verticals:

- **BAP / Torget** — used goods
- **Real estate** — homes for sale and rentals
- **Cars** — used (new cars are only available via the official API)
- **Jobs** — full-time listings

## What this fork changes

The server has been running continuously in a container on a NAS, behind a
reverse proxy, answering job-ad queries for a production application. Nearly
everything below comes from that: things that only surface once software runs
unattended for weeks rather than in a terminal for an afternoon.

- **Search results are read from the data finn.no ships, not the markup.**
  Search pages carry the results as JSON alongside the rendered page. Reading
  that instead of walking the DOM returns 53 results where card scraping
  returned 37 off the same page, with a location on every one instead of
  none, plus the real total (13723 matching ads, not "53") and fields the
  markup never showed -- make, model, mileage, employer, deadline. Card
  scraping remains as a fallback and still handles real estate.
- **Job ads read from the DOM when the JSON-LD disappears.** Since around
  April 2026 finn.no stopped shipping a schema.org `JobPosting` on job ads.
  Every job listing came back with `description=None` while the text sat in
  the markup, and that failure was indistinguishable from an ad that really
  had no description. JSON-LD is still tried first.
- **Requests bounded per minute, not just serialized** — see the credit note
  below; this one is a strengthening rather than an addition.
- **The cache no longer grows without limit.** Expired rows are swept, the
  file is capped, and saved-search history is bounded.
- **Raw HTML is no longer stored by default.** Every fetched page used to be
  persisted in full and kept indefinitely, which accumulated other people's ad
  text and contact details on disk. Now opt-in via `FINN_CACHE_RAW_HTML`.
- **Block pages fail loudly.** A CAPTCHA or "tilgang nektet" interstitial is
  valid HTML, so it used to parse to an empty result list — indistinguishable
  from a search with no matches, and the natural retry made things worse.
- **`get_listing` accepts a finn.no URL** and takes the vertical from its
  path, instead of probing up to five verticals to find out.
- **Search arguments are validated** before they become a finn.no query
  string, because the caller is a language model rather than a form.
- **Operational limits are environment variables**, so retuning them does not
  mean rebuilding the container image.
- **`discover_filters`**, so the filter parameters do not have to be guessed.
  finn.no offers 16 filters on jobs and 24 on cars, keyed by codes like
  `location=1.20001.20061`; this reports them with hit counts.
- **Five operational tools** — `discover_filters`, `get_server_status`,
  `clear_cache`, `export_saved_searches`, `import_saved_searches`.
- **Silent degradation is visible.** `get_server_status` reports, per
  vertical, whether searches were answered from the embedded data or from
  card scraping, because the failure this project has already lived through
  was not a crash -- it was months of answers that merely had nothing in them.
- **41 tests and one fixture**, including the real job ad that broke the
  parser, so the regression is caught rather than remembered.

### Credit where it is due

**Rate limiting was already here.** The upstream project had `RateLimitedError`,
a semaphore serializing requests, and a polite randomized delay between them.
Saying this fork "added rate limiting" would be false.

What it adds is a sliding window that enforces a request-per-minute ceiling —
a semaphore bounds how many requests are in flight, which is not the same as
how many are sent per minute — plus exponential backoff, retries on network
errors, and reading the actual `Retry-After` header instead of assuming 60
seconds.

Equally: the scraper architecture, the `FinnBackend` seam, the JSON-LD
parsing, the SQLite cache, saved searches and the original six MCP tools are
all upstream work. This fork did not design any of it.

**Reading the embedded search data is not an original idea either.** It comes
from [viktorfa/finn_mcp](https://github.com/viktorfa/finn_mcp), an unrelated
MCP server for finn.no written in Node. No code was taken — that project is
JavaScript and this is Python — but the approach is theirs, and it is a
better approach than the DOM walking that was here.

## Tools

| Tool | Purpose |
|------|---------|
| `search_finn` | Search a vertical by keyword + optional filters. |
| `discover_filters` | List the filters finn.no offers for a vertical, with hit counts. |
| `get_listing` | Fetch a full listing by `finnkode` or finn.no URL. |
| `save_search` | Persist a named recurring search. |
| `list_saved_searches` | List all saved searches. |
| `delete_saved_search` | Remove a saved search. |
| `check_saved_search` | Run a saved search and return only hits that are new since the last check. |
| `clear_cache` | Delete cached listings while preserving saved searches. |
| `get_server_status` | Show backend, privacy, cache, and rate-limit settings. |
| `export_saved_searches` | Export portable saved-search data. |
| `import_saved_searches` | Restore exported saved searches. |

## Install & run

Requires [uv](https://docs.astral.sh/uv/). To run **this fork** without
cloning or installing anything permanently:

```bash
uvx --from git+https://github.com/tomaszmroczynski/finn-mcp finn-mcp
```

Note that plain `uvx finn-mcp` installs the upstream package from PyPI, which
is the original project and does not contain the changes listed above.

## Register with Claude Code

```bash
claude mcp add finn-mcp -- uvx --from git+https://github.com/tomaszmroczynski/finn-mcp finn-mcp
```

Or add to your `.mcp.json` (or Claude Desktop's `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "finn-mcp": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/tomaszmroczynski/finn-mcp",
        "finn-mcp"
      ]
    }
  }
}
```

## Data access

There is no self-serve public finn.no API. For v1 this server scrapes the
public web pages and parses JSON-LD on detail pages. A stable `FinnBackend`
interface separates the MCP surface from the data source, so the scraper can
be swapped for the official partner API (`cache.api.finn.no/iad/`) later.

Backend selection is controlled by the `FINN_BACKEND` environment variable:

- `FINN_BACKEND=scraper` (default) — the scraper implementation.
- `FINN_BACKEND=official` — stub; raises `NotImplementedError` until
  partner credentials are wired up.

Parsed responses are cached for 24 hours in a local SQLite database at
`$XDG_DATA_HOME/finn-mcp/cache.sqlite` (defaults to
`~/.local/share/finn-mcp/cache.sqlite`). Raw HTML is not stored by default.
Set `FINN_CACHE_RAW_HTML=1` only for local debugging.

The scraper serializes requests, defaults to 20 requests per minute, honors
`Retry-After`, and retries temporary network/5xx failures with exponential
backoff. Relevant settings can be adjusted with:

- `FINN_REQUESTS_PER_MINUTE` (default `20`)
- `FINN_HTTP_MAX_RETRIES` (default `3`)
- `FINN_HTTP_TIMEOUT_SECONDS` (default `15`)
- `FINN_MAX_SAVED_FINNKODES` (default `1000`)
- `FINN_MAX_CACHE_BYTES` (default `262144000`, or 250 MiB)
- `FINN_USE_DEHYDRATED_STATE` (default `1`) — read results from the embedded
  search data; set to `0` to fall back to card scraping everywhere
- `FINN_DEHYDRATED_STATE_STRICT` (default off) — raise instead of falling back
  when the embedded data is missing. For CI, so a change at finn.no fails a
  build rather than quietly returning sparser results.

`search_finn` returns a `SearchResponse`: `results` for the page, plus
`total_matches` and `last_page` for the whole result set, `search_url` for the
request that was made, and `source` (`state` or `cards`) saying which path
produced it. Real estate is always `cards`, and reports no totals rather than
reporting the page length as one.

`get_listing` accepts either a finnkode or a full `finn.no` listing URL.
Providing a URL or explicit vertical avoids category-probing requests.

This is an unofficial scraper, not a FINN.no product. Confirm that your use
complies with FINN.no's current terms before sustained or commercial use.

## Develop from source

```bash
git clone https://github.com/tomaszmroczynski/finn-mcp
cd finn-mcp
uv sync
uv run pytest
uv run finn-mcp   # stdio server
```

Tests run against saved HTML fixtures in `tests/fixtures/` and do not hit
finn.no over the network. The contact name in the job-ad fixture has been
replaced with a placeholder; no assertion depends on it.

## License

MIT, unchanged from upstream. See [LICENSE](./LICENSE). Copyright for the
original work remains with its authors; the changes described above are
contributed under the same licence.
