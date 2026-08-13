"""Trimming, deduping and the in-process TTL cache.

The dedupe rules encode two measured facts: 970 rows carry only 888 unique
identifiers even in a single language, and requesting two languages returns each
topic once per language (970 + 536 = 1506).
"""

import httpx
import respx

from app.services import corpus
from tests.conftest import PORTAL_URL

# --- reading the Portal's dict-of-lists metadata ------------------------------


def test_first_unwraps_single_element_lists():
    assert corpus._first({"status": ["31094502"]}, "status") == "31094502"
    assert corpus._first({"status": []}, "status") is None
    assert corpus._first({}, "status") is None
    assert corpus._first({"status": "raw"}, "status") == "raw"


def test_all_reads_repeated_fields():
    assert corpus._all({"tags": ["a", "b", None]}, "tags") == ["a", "b"]
    assert corpus._all({"tags": "solo"}, "tags") == ["solo"]
    assert corpus._all({}, "tags") == []


def test_plain_text_strips_markup_and_entities():
    assert corpus._plain_text("<p>Expected&nbsp;Outcome</p>  <b>x</b>") == "Expected Outcome x"
    assert corpus._plain_text(None) == ""


def test_nested_json_parses_fields_stored_as_strings():
    """actions/budgetOverview/links arrive as JSON *text* inside a 1-element list."""
    metadata = {"actions": ['[{"status": {"abbreviation": "Open"}}]']}
    assert corpus._nested_json(metadata, "actions")[0]["status"]["abbreviation"] == "Open"


def test_nested_json_treats_unusable_content_as_absent():
    assert corpus._nested_json({"actions": ["{not json"]}, "actions") is None
    assert corpus._nested_json({"actions": [""]}, "actions") is None
    assert corpus._nested_json({"actions": [{"already": "parsed"}]}, "actions") is None
    assert corpus._nested_json({}, "actions") is None


def test_deadlines_fall_back_to_the_budget_overview():
    """Some rows carry no deadlineDate but do carry deadlines inside the
    budgetOverview JSON blob."""
    metadata = {
        "budgetOverview": [
            '{"budgetTopicActionMap": {"1": [{"deadlineDates": ["2026-09-05", "2027-02-01"]}]}}'
        ]
    }
    assert corpus._deadlines(metadata) == ["2026-09-05", "2027-02-01"]
    assert corpus._deadlines({}) == []


# --- trimming ----------------------------------------------------------------


def test_trim_flattens_a_real_row(portal_page, identifiers):
    row = next(
        r for r in portal_page["results"] if r["metadata"]["identifier"][0] == identifiers["cl2"]
    )
    topic = corpus.trim(row)
    assert topic["identifier"] == identifiers["cl2"]
    assert isinstance(topic["title"], str) and topic["title"]
    assert topic["status"] in {"31094501", "31094502"}
    assert topic["language"] == "en"
    assert "<" not in topic["description"]  # HTML stripped for keyword matching
    assert isinstance(topic["keywords"], list)


def test_trim_tolerates_a_row_missing_optional_keys(portal_page, identifiers):
    """destination* is present on only 82 of 100 rows."""
    row = next(
        r
        for r in portal_page["results"]
        if r["metadata"]["identifier"][0] == identifiers["no_destination"]
    )
    topic = corpus.trim(row)
    assert topic["destination"] is None
    assert topic["identifier"] == identifiers["no_destination"]


def test_trim_of_an_empty_row_does_not_raise():
    topic = corpus.trim({})
    assert topic["identifier"] == ""
    assert topic["description"] == ""


# --- dedupe ------------------------------------------------------------------


def test_duplicate_identifiers_collapse(topics, portal_page):
    """970 rows carry only 888 identifiers, so this runs on every fetch."""
    assert len(topics) == len(portal_page["results"])
    deduped = corpus._dedupe(topics, ["en"])
    assert len(deduped) == len({t["identifier"] for t in topics})
    assert len(deduped) < len(topics)


def test_language_preference_with_per_topic_fallback():
    """da covers only 536 of the 970 English topics, so ["da","en"] has to fall
    back to English per topic rather than dropping the topic."""
    rows = [
        {"identifier": "A", "language": "en", "status": "1", "description": "english A"},
        {"identifier": "A", "language": "da", "status": "1", "description": "dansk A"},
        {"identifier": "B", "language": "en", "status": "1", "description": "english B"},
    ]
    deduped = corpus._dedupe(rows, ["da", "en"])
    picked = {t["identifier"]: t["language"] for t in deduped}
    assert picked == {"A": "da", "B": "en"}


def test_a_stub_never_displaces_a_complete_row():
    """The HORIZON-CL5-2024-D6-01-05 case: the stub sorted first by relevance."""
    stub = {"identifier": "A", "language": "en", "status": None, "description": ""}
    real = {"identifier": "A", "language": "en", "status": "1", "description": "real"}
    assert corpus._dedupe([stub, real], ["en"])[0] is real


def test_rows_without_an_identifier_are_dropped():
    assert corpus._dedupe([{"identifier": "", "language": "en"}], ["en"]) == []


def test_unrequested_languages_sort_last():
    rows = [
        {"identifier": "A", "language": "fr", "status": "1", "description": "fr"},
        {"identifier": "A", "language": "en", "status": "1", "description": "en"},
    ]
    assert corpus._dedupe(rows, ["en"])[0]["language"] == "en"


# --- the TTL cache -----------------------------------------------------------


QUERY = {"bool": {"must": [{"terms": {"type": ["1"]}}]}}


@respx.mock
async def test_corpus_is_fetched_once_then_served_from_memory(portal_page):
    route = respx.post(PORTAL_URL).mock(return_value=httpx.Response(200, json=portal_page))
    first = await corpus.get_topics(QUERY, ["en"])
    second = await corpus.get_topics(QUERY, ["en"])
    assert first == second
    assert route.call_count == 1  # no second round-trip


@respx.mock
async def test_corpus_refetches_once_the_ttl_expires(portal_page, monkeypatch):
    route = respx.post(PORTAL_URL).mock(return_value=httpx.Response(200, json=portal_page))
    clock = [1000.0]
    # monotonic, not wall-clock: a clock change must not make a fresh cache stale.
    monkeypatch.setattr(corpus.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(corpus.settings, "portal_cache_ttl", 60)

    await corpus.get_topics(QUERY, ["en"])
    clock[0] += 59
    await corpus.get_topics(QUERY, ["en"])
    assert route.call_count == 1
    clock[0] += 2  # now past the TTL
    await corpus.get_topics(QUERY, ["en"])
    assert route.call_count == 2


@respx.mock
async def test_a_different_language_is_a_different_cache_entry(portal_page):
    """Keyed on the query alone, a "da" request would be served the "en" corpus."""
    route = respx.post(PORTAL_URL).mock(return_value=httpx.Response(200, json=portal_page))
    await corpus.get_topics(QUERY, ["en"])
    await corpus.get_topics(QUERY, ["da"])
    assert route.call_count == 2


@respx.mock
async def test_a_different_query_is_a_different_cache_entry(portal_page):
    route = respx.post(PORTAL_URL).mock(return_value=httpx.Response(200, json=portal_page))
    await corpus.get_topics(QUERY, ["en"])
    await corpus.get_topics({"bool": {"must": []}}, ["en"])
    assert route.call_count == 2


@respx.mock
async def test_invalidate_drops_the_cache(portal_page):
    route = respx.post(PORTAL_URL).mock(return_value=httpx.Response(200, json=portal_page))
    await corpus.get_topics(QUERY, ["en"])
    corpus.invalidate()
    await corpus.get_topics(QUERY, ["en"])
    assert route.call_count == 2
