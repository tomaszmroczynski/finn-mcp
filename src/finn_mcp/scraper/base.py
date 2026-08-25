from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any

from selectolax.parser import HTMLParser, Node

from .. import config, http_client
from ..models import Listing, SearchResult, Vertical
from . import dehydrated

log = logging.getLogger(__name__)

_WHITESPACE_RE = re.compile(r"\s+")
_BLOCK_MARKERS = (
    "captcha",
    "access denied",
    "unusual traffic",
    "verify you are human",
    "tilgang nektet",
)


class PageBlockedError(RuntimeError):
    pass


class MissingSearchStateError(RuntimeError):
    """Raised only under DEHYDRATED_STATE_STRICT, so CI notices a change."""

    def __init__(self, vertical: str):
        super().__init__(f"no embedded search state on the {vertical} search page")
        self.vertical = vertical


def validate_page(html: str) -> None:
    sample = html[:100_000].lower()
    if len(html.strip()) < 100 or any(marker in sample for marker in _BLOCK_MARKERS):
        raise PageBlockedError("finn.no returned a block, CAPTCHA, or incomplete page")


def _clean(text: str | None) -> str | None:
    if text is None:
        return None
    text = text.replace("\xa0", " ").replace("\u200b", "")
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text or None


def parse_price_nok(text: str | None) -> int | None:
    """Extract an NOK price from text like '4 200 000 kr' or 'kr 17 500'.

    Tolerates pipes/other separators between the amount and the 'kr' token —
    search-result cards sometimes render them as ``'400 000 |  | kr'``.
    Returns int number of NOK, or None if no price found.
    """
    if not text:
        return None
    compact = text.replace("\xa0", " ").replace(",", "")
    # Allow arbitrary non-digit junk (including pipes) between amount and "kr".
    m = re.search(r"(\d[\d\s]{2,})\s*[^\d]{0,6}\s*(?:kr|NOK)\b", compact, flags=re.IGNORECASE)
    if not m:
        m = re.search(r"(?:kr|NOK)\b\s*[^\d]{0,6}(\d[\d\s]{2,})", compact, flags=re.IGNORECASE)
    if not m:
        return None
    digits = re.sub(r"\s+", "", m.group(1))
    try:
        return int(digits)
    except ValueError:
        return None


def walk_to_article(node: Node, max_steps: int = 12) -> Node | None:
    cur = node
    for _ in range(max_steps):
        if cur is None:
            return None
        if cur.tag == "article":
            return cur
        cur = cur.parent
    return None


def first_image_src(article: Node) -> str | None:
    img = article.css_first("img")
    if not img:
        return None
    attrs = img.attributes
    src = attrs.get("src") or ""
    if src and not src.startswith("data:"):
        return src
    srcset = attrs.get("srcset") or ""
    if srcset:
        first = srcset.split(",")[0].strip().split(" ")[0]
        if first:
            return first
    return None


class VerticalScraper(ABC):
    vertical: Vertical
    link_pattern: str  # substring that distinguishes this vertical's detail links
    finnkode_re: re.Pattern[str]
    # Prefix of finn.no's own search_key for this vertical. None means the
    # vertical has no embedded search state (real estate) and card scraping
    # is the only path.
    search_key_prefix: str | None = None

    @abstractmethod
    def search_url(self, query: str, page: int, filters: dict[str, str] | None) -> tuple[str, dict[str, str]]:
        """Return (base_url, query_params) for a search request."""

    @abstractmethod
    def detail_url(self, finnkode: str) -> str:
        ...

    @abstractmethod
    def parse_detail(self, finnkode: str, html: str) -> Listing:
        ...

    async def search(
        self, query: str, page: int = 1, filters: dict[str, str] | None = None
    ) -> list[SearchResult]:
        url, params = self.search_url(query, page, filters)
        html = await http_client.fetch(url, params=params)
        validate_page(html)

        if config.USE_DEHYDRATED_STATE:
            payload = dehydrated.find_search_payload(html, self.search_key_prefix)
            if payload is not None:
                results = [
                    r for r in (self._result_from_doc(d) for d in payload.docs)
                    if r is not None
                ]
                if results:
                    return results
                log.warning(
                    "%s: embedded state present but produced no results; "
                    "falling back to card scraping",
                    self.vertical,
                )
            elif self.search_key_prefix is not None:
                log.warning(
                    "%s: expected embedded search state and found none; "
                    "falling back to card scraping",
                    self.vertical,
                )
                if config.DEHYDRATED_STATE_STRICT:
                    raise MissingSearchStateError(self.vertical)

        return self.parse_search_cards(html)

    def _result_from_doc(self, doc: dict[str, Any]) -> SearchResult | None:
        """Map one entry of finn.no's own search payload to a SearchResult.

        The shared fields are named identically across verticals, so only the
        per-vertical extras are left to subclasses.
        """
        finnkode = str(doc.get("id") or doc.get("ad_id") or "")
        title = _clean(doc.get("heading"))
        if not finnkode.isdigit() or not title:
            return None
        price = doc.get("price")
        price = price if isinstance(price, dict) else {}
        image = doc.get("image")
        image = image if isinstance(image, dict) else {}
        image_urls = doc.get("image_urls")
        amount = price.get("amount")
        url = doc.get("canonical_url") or self.detail_url(finnkode)
        return SearchResult(
            finnkode=finnkode,
            vertical=self.vertical,
            title=title,
            price=amount if isinstance(amount, int) else None,
            currency=price.get("currency_code") or "NOK",
            location=_clean(doc.get("location")),
            thumbnail_url=image.get("url")
            or (image_urls[0] if isinstance(image_urls, list) and image_urls else None),
            url=url,
            extra=self._doc_extras(doc),
        )

    def _doc_extras(self, doc: dict[str, Any]) -> dict[str, Any]:
        """Per-vertical fields worth surfacing from a search payload entry."""
        return {}

    async def fetch_detail(self, finnkode: str) -> tuple[str, Listing]:
        url = self.detail_url(finnkode)
        html = await http_client.fetch(url)
        validate_page(html)
        listing = self.parse_detail(finnkode, html)
        if not listing.title or listing.title.endswith(f"listing {finnkode}"):
            raise ValueError("listing page did not contain the required title")
        return html, listing

    def parse_search_cards(self, html: str) -> list[SearchResult]:
        tree = HTMLParser(html)
        seen_finnkodes: set[str] = set()
        seen_articles: set[int] = set()
        results: list[SearchResult] = []
        for a in tree.css(f'a[href*="{self.link_pattern}"]'):
            href = a.attributes.get("href") or ""
            m = self.finnkode_re.search(href)
            if not m:
                continue
            finnkode = m.group(1)
            if finnkode in seen_finnkodes:
                continue
            article = walk_to_article(a)
            if article is None or id(article) in seen_articles:
                continue
            seen_finnkodes.add(finnkode)
            seen_articles.add(id(article))
            result = self._parse_card(finnkode, href, article)
            if result is not None:
                results.append(result)
        return results

    def _parse_card(self, finnkode: str, href: str, article: Node) -> SearchResult | None:
        text = article.text(separator=" | ", strip=True)
        text = text.replace("\xa0", " ") if text else ""
        title = self._guess_title(article, text)
        if not title:
            return None
        price = parse_price_nok(text)
        location = self._guess_location(article, text)
        thumbnail = first_image_src(article)
        url = href if href.startswith("http") else f"https://www.finn.no{href}"
        extra = self._card_extras(article, text)
        return SearchResult(
            finnkode=finnkode,
            vertical=self.vertical,
            title=title,
            price=price,
            location=location,
            thumbnail_url=thumbnail,
            url=url,
            extra=extra,
        )

    # ----- hooks subclasses can override for card quirks -----

    def _guess_title(self, article: Node, text: str) -> str | None:
        h = article.css_first("h2") or article.css_first("h3")
        if h:
            t = _clean(h.text(separator=" ", strip=True))
            if t:
                return t
        # Fallback: first non-trivial text segment
        for part in text.split(" | "):
            cleaned = _clean(part)
            if cleaned and len(cleaned) > 5 and "kr" not in cleaned.lower():
                return cleaned
        return None

    def _guess_location(self, article: Node, text: str) -> str | None:
        return None

    def _card_extras(self, article: Node, text: str) -> dict[str, Any]:
        return {}


def now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)
