"""Retrieve, trim and cache the set of call topics a profile can rank over.

Why a corpus rather than per-request queries: the Portal cannot filter on a
topic identifier (its query DSL rejects ``wildcard``/``match``), so the profile's
``topic_contains``/``clusters`` rules must run client-side — and ranking is only
honest if it sees *every* topic, not the first relevance-ordered page. The
default profile is 970 rows across 10 requests; page 1 of 100 contains zero
"MISS" topics, so a paged-per-request design would make the README's example
profile look broken.

Fetching all of it is 20.8 MB, but 36% of that is HTML we never use
(``topicConditions``, ``supportInfo``) — trimming at ingest brings the retained
corpus to roughly 1 MB of metadata plus the plain-text descriptions that keyword
ranking needs.

**Where the cache lives**: behind ``app.services.cache``, which picks a backend
from ``CACHE_BACKEND``. With the default ``memory`` backend it is the uvicorn
process heap — it survives across requests within one process and nothing else,
so a restart, a redeploy or any ``--reload`` in dev drops it and the next request
re-pays the cold fetch, and each worker holds its own copy. Set
``CACHE_BACKEND=redis`` to share one corpus across instances and across
restarts. Either way a cache failure only costs a refetch: the backend fails
open.
"""

import html
import json
import logging
import re
from typing import Any

from app.config import settings
from app.services import cache, portal_api

log = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

# The cache namespace for this module. Bumping cache._NAMESPACE_VERSION is what
# invalidates every entry after a change to the shape trim() returns.
_NAMESPACE = "corpus"


def _cache_key(query: dict, languages: list[str]) -> str:
    """Key one corpus on its server query plus the languages fetched.

    The language MUST be part of the key: keyed on the query alone, a request
    for "da" would silently be served the previously cached "en" corpus.
    """
    return cache.make_key(_NAMESPACE, json.dumps(query, sort_keys=True), ",".join(languages))


def _first(metadata: dict[str, Any], key: str) -> Any:
    """Read a scalar out of the Portal's dict-of-lists metadata.

    Every metadata value is an array, even conceptually scalar ones — ``status``
    arrives as ``["31094502"]``. Not all keys are present on every row, so this
    returns None rather than raising.
    """
    value = metadata.get(key)
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _all(metadata: dict[str, Any], key: str) -> list[str]:
    """Read a genuinely repeated metadata field as a list of strings."""
    value = metadata.get(key)
    if isinstance(value, list):
        return [str(v) for v in value if v]
    return [str(value)] if value else []


def _plain_text(value: str | None) -> str:
    """Strip HTML tags/entities and collapse whitespace to bare text."""
    if not value:
        return ""
    return _WS_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", value))).strip()


def _nested_json(metadata: dict[str, Any], key: str) -> Any:
    """Parse one of the metadata fields that holds JSON *as a string*.

    ``actions``, ``budgetOverview``, ``links`` and ``latestInfos`` are each a
    single-element list containing a JSON document encoded as text, so reading
    them takes an unwrap plus a parse. Malformed content is treated as absent —
    a depth field is never worth failing a search over.
    """
    raw = _first(metadata, key)
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        log.debug("could not parse metadata field %s as JSON", key)
        return None


def _keywords(metadata: dict[str, Any]) -> list[str]:
    """Read ``keywords``, which the Portal returns in **two different shapes**.

    Most rows carry a plain list of strings. Some carry a single-element list
    whose one element is a JSON array encoded *as text* — the same
    encoded-document trick used by ``actions``/``budgetOverview``/``links``, but
    undocumented here because the field looks like an ordinary list.

    Left unparsed, that row's keywords are one ~1 KB escaped string: the caller
    gets a `list[str]` containing JSON instead of keywords, and
    ``results.compact()``'s ``keywords[:MCP_MAX_KEYWORDS]`` silently caps a
    1-element list, so the whole blob reaches the model on every chat turn.
    Ranking is unaffected either way — substring matching finds the terms inside
    the blob — which is why this stayed invisible.

    3 of the 9 rows in ``tests/fixtures/portal_search_page.json`` are the
    encoded shape, so both branches are exercised by the real fixture.
    """
    values = _all(metadata, "keywords")
    if len(values) == 1 and values[0].lstrip().startswith("["):
        parsed = _nested_json(metadata, "keywords")
        if isinstance(parsed, list):
            return [str(v) for v in parsed if v]
    return values


def _deadlines(metadata: dict[str, Any]) -> list[str]:
    """Every deadline for the topic, preferring the explicit metadata field."""
    deadlines = _all(metadata, "deadlineDate")
    if deadlines:
        return deadlines
    budget = _nested_json(metadata, "budgetOverview") or {}
    found: list[str] = []
    for actions in (budget.get("budgetTopicActionMap") or {}).values():
        for action in actions or []:
            found.extend(str(d) for d in (action.get("deadlineDates") or []))
    return found


def trim(row: dict[str, Any]) -> dict[str, Any]:
    """Reduce one raw search hit to the fields the product actually uses.

    Drops ``topicConditions``/``supportInfo``/``es*`` (36% of the payload, no
    product value) and flattens the dict-of-lists. ``description`` keeps the full
    stripped text rather than a truncated preview, because keyword ranking reads
    it — roughly half the keyword signal lives only in the description.
    """
    metadata = row.get("metadata") or {}
    deadlines = _deadlines(metadata)
    return {
        "identifier": str(_first(metadata, "identifier") or ""),
        "title": str(_first(metadata, "title") or ""),
        "status": _first(metadata, "status"),
        "sort_status": _first(metadata, "sortStatus"),
        "deadline_date": deadlines[0] if deadlines else None,
        "deadline_dates": deadlines,
        "deadline_model": _first(metadata, "deadlineModel"),
        "start_date": _first(metadata, "startDate"),
        "call_identifier": _first(metadata, "callIdentifier"),
        "call_title": _first(metadata, "callTitle"),
        "framework_programme": _first(metadata, "frameworkProgramme"),
        "programme_division": _all(metadata, "programmeDivision"),
        "types_of_action": _all(metadata, "typesOfAction"),
        "destination": _first(metadata, "destination"),
        "keywords": _keywords(metadata),
        "tags": _all(metadata, "tags"),
        "url": _first(metadata, "url") or row.get("url"),
        "language": row.get("language"),
        "description": _plain_text(_first(metadata, "descriptionByte")),
        "reference": row.get("reference"),
    }


def _is_complete(topic: dict[str, Any]) -> bool:
    """True unless this is an index stub (no status, or no description text)."""
    return bool(topic.get("status")) and bool(topic.get("description"))


def _dedupe(topics: list[dict[str, Any]], languages: list[str]) -> list[dict[str, Any]]:
    """Collapse to one row per identifier, choosing the best available row.

    Two distinct kinds of duplicate exist and both are handled here:

    * **Cross-language.** A topic is indexed once per language, so requesting
      ``["en","da"]`` returns each topic twice (970 + 536 = 1506 rows for the
      default profile). Sorting on the requested language's position gives a
      preference order with per-topic fallback: ``["da","en"]`` yields Danish
      where it exists and English otherwise.
    * **Exact duplicates and stubs.** Even a single-language fetch repeats some
      topics — 970 rows carry only 888 identifiers — and some rows are stubs
      with no status or description. Completeness is the tiebreak so a stub can
      never displace a real record.
    """
    order = {language: index for index, language in enumerate(languages)}
    fallback = len(order)

    def rank(topic: dict[str, Any]) -> tuple[int, int]:
        return (order.get(str(topic.get("language")), fallback), 0 if _is_complete(topic) else 1)

    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    # sorted() is stable, so rows of equal rank keep their original relevance order.
    for topic in sorted(topics, key=rank):
        identifier = str(topic.get("identifier") or "")
        if not identifier or identifier in seen:
            continue
        seen.add(identifier)
        out.append(topic)
    return out


async def get_topics(query: dict, languages: list[str]) -> list[dict[str, Any]]:
    """Return the trimmed, deduped topic corpus for one query + language set.

    Served from the cache backend when an entry is present and younger than
    ``PORTAL_CACHE_TTL``; otherwise refetched and stored. A backend that is
    unreachable simply yields a miss, so the worst a cache outage can do is make
    the search slow.
    """
    key = _cache_key(query, languages)
    cached = await cache.get_json(key)
    if cached is not None:
        return cached

    rows = await portal_api.search_all(query, languages=languages)
    topics = _dedupe([trim(row) for row in rows], languages)
    log.info(
        "loaded topic corpus: %d rows -> %d unique topics (languages=%s)",
        len(rows),
        len(topics),
        ",".join(languages),
    )
    await cache.set_json(key, topics, settings.portal_cache_ttl)
    return topics
