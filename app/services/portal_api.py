"""HTTP client for the EU Funding & Tenders Portal SEDIA search API.

The endpoint is public — ``apiKey=SEDIA`` is the literal key the portal's own
frontend uses — but its request encoding is unusual enough that the obvious
implementation fails; see ``_blob`` below.

Only ``bool``/``must``/``terms`` are accepted in a query. ``wildcard`` and
``match`` come back as ``400 businessError: "Invalid Query format"``, which is
why identifier substring filtering lives in ``services/profile.py`` instead.
"""

import json
import logging
from typing import Any

import httpx

from app.config import settings

log = logging.getLogger(__name__)

# A single shared client is reused across requests so TCP connections are
# pooled rather than reopened each time. It is created lazily (on first use)
# and held in this module-level variable.
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    """Return the shared HTTP client, creating it on first call (lazy singleton)."""
    global _client  # rebind the module-level variable, not a local one
    if _client is None:
        _client = httpx.AsyncClient(
            # A corpus refresh is 10 sequential page requests, so the read
            # timeout is per-request while connect stays short.
            timeout=httpx.Timeout(settings.portal_timeout, connect=5.0),
        )
    return _client


async def close_client() -> None:
    """Close the shared client and reset it (called on app shutdown)."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _blob(payload: Any) -> tuple[str, str, str]:
    """Encode one JSON parameter as a multipart *file* part.

    DO NOT "simplify" this into a plain form field or a URL parameter. The API
    requires ``query`` and ``languages`` to arrive as file parts, each with a
    filename and an explicit ``Content-Type: application/json`` — this mirrors
    the portal frontend, which posts them as ``FormData(Blob)``. Sent as an
    ordinary form field, the request fails with ``500 {"type":"throwable"}``;
    passed in the URL, ``query`` is accepted and then *silently ignored*, so the
    call appears to work while returning unfiltered results. Both failure modes
    cost an afternoon to diagnose.
    """
    return ("blob", json.dumps(payload), "application/json")


async def search(
    query: dict,
    *,
    languages: list[str],
    text: str = "***",
    page_size: int | None = None,
    page_number: int = 1,
) -> dict[str, Any]:
    """POST one page of search results.

    ``text="***"`` is the wildcard the portal uses to mean "no free-text term";
    the real filtering happens in ``query``.
    """
    params = {
        "apiKey": settings.portal_api_key,
        "text": text,
        "pageSize": page_size or settings.portal_page_size,
        "pageNumber": page_number,
    }
    files = {"query": _blob(query), "languages": _blob(languages)}
    response = await _get_client().post(settings.portal_search_url, params=params, files=files)
    response.raise_for_status()  # turn a 4xx/5xx into an exception
    return response.json()


async def search_all(
    query: dict,
    *,
    languages: list[str],
    max_pages: int | None = None,
) -> list[dict[str, Any]]:
    """Page through every result for ``query`` and return the raw rows.

    Stops on the first short page (the last one) or at ``portal_max_pages``.
    Hitting the cap is logged rather than passed over in silence: a truncated
    corpus would make the ranking quietly incomplete.
    """
    limit = max_pages or settings.portal_max_pages
    page_size = settings.portal_page_size
    rows: list[dict[str, Any]] = []
    for page_number in range(1, limit + 1):
        payload = await search(
            query, languages=languages, page_size=page_size, page_number=page_number
        )
        results = payload.get("results") or []
        rows.extend(results)
        if len(results) < page_size:
            return rows  # short page → that was the last one
        total = payload.get("totalResults")
        if isinstance(total, int) and len(rows) >= total:
            return rows
    log.warning(
        "stopped paging at max_pages=%d (%d rows); the corpus may be incomplete — "
        "raise PORTAL_MAX_PAGES if the profile legitimately matches more",
        limit,
        len(rows),
    )
    return rows


def _has_content(row: dict[str, Any]) -> bool:
    """True if a row carries real topic data rather than being an index stub.

    Several documents can share one identifier and some are stubs: for
    ``HORIZON-CL5-2024-D6-01-05`` the row that sorted *first* by relevance had no
    status, no deadline and an empty description. Picking ``results[0]`` would
    return that one.
    """
    metadata = row.get("metadata") or {}
    description = metadata.get("descriptionByte") or []
    return bool(metadata.get("status")) and bool(description and description[0])


def _best_row(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pick the richest row from a set sharing one identifier."""
    if not rows:
        return None
    for row in rows:
        if _has_content(row):
            return row
    return rows[0]  # all stubs — return something rather than nothing


async def get_by_identifier(identifier: str, *, languages: list[str]) -> dict[str, Any] | None:
    """Fetch one topic by its exact identifier, or None if it isn't found."""
    # Imported here rather than at module scope: profile imports config, and
    # keeping the dependency one-way avoids a cycle if profile ever needs this.
    from app.services.profile import identifier_query

    payload = await search(
        identifier_query(identifier), languages=languages, page_size=10, page_number=1
    )
    return _best_row(payload.get("results") or [])


async def ping() -> bool:
    """Readiness probe — a minimal search against the Portal.

    Deliberately cheap (``pageSize=1``) and deliberately *not* a corpus warm-up:
    readiness should answer "can we reach the Portal", not pull 20 MB.
    """
    try:
        payload = await search(
            {"bool": {"must": [{"terms": {"type": ["1"]}}]}},
            languages=settings.portal_languages,
            page_size=1,
        )
        return "results" in payload
    except httpx.HTTPError:
        # Any network/HTTP error means "not ready"; swallow it and report False.
        return False
