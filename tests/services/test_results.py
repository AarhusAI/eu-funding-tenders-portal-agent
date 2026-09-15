from app.models import SearchResponse, TopicSummary
from app.services import results


def _summary(**overrides) -> TopicSummary:
    base = {
        "identifier": "HORIZON-CL5-2027-01-D1-10",
        "title": "A topic",
        "description": "x" * 900,
        "keywords": [f"k{n}" for n in range(20)],
        "tags": [f"t{n}" for n in range(20)],
    }
    return TopicSummary(**{**base, **overrides})


def test_numeric_codes_are_translated_for_humans():
    summary = results._to_summary(
        {
            "identifier": "HORIZON-CL5-1",
            "title": "T",
            "status": "31094502",
            "framework_programme": "43108390",
        }
    )
    assert summary.status == "Open for submission"
    assert summary.programme == "Horizon Europe"


def test_unknown_codes_pass_through_rather_than_vanishing():
    """A new Portal code should show up as itself, not as None."""
    summary = results._to_summary(
        {"identifier": "X", "title": "T", "status": "99999999", "framework_programme": "88888888"}
    )
    assert summary.status == "99999999"
    assert summary.programme == "88888888"


def test_absent_codes_stay_none():
    summary = results._to_summary({"identifier": "X", "title": "T"})
    assert summary.status is None
    assert summary.programme is None


def test_summary_of_an_empty_topic_does_not_raise():
    summary = results._to_summary({})
    assert summary.identifier == ""
    assert summary.score == 0


def test_compact_truncates_long_descriptions_for_llm_callers():
    """OpenWebUI replays every tool result on every turn, so an untrimmed
    payload accumulates until it overflows the model's context."""
    response = SearchResponse(results=[_summary()], total_matched=5)
    compacted = results.compact(response)
    description = compacted.results[0].description
    assert len(description) == results.settings.mcp_max_description_chars + 1  # + the ellipsis
    assert description.endswith("…")
    assert len(compacted.results[0].keywords) == results.settings.mcp_max_keywords
    assert len(compacted.results[0].tags) == results.settings.mcp_max_keywords
    assert compacted.total_matched == 5


def test_the_compaction_bounds_are_configurable(monkeypatch):
    """The reviewer's point: these caps are a deployment decision, not a constant."""
    monkeypatch.setattr(results.settings, "mcp_max_description_chars", 20)
    monkeypatch.setattr(results.settings, "mcp_max_keywords", 2)
    compacted = results.compact(SearchResponse(results=[_summary()]))
    assert len(compacted.results[0].description) == 21  # 20 + the ellipsis
    assert len(compacted.results[0].keywords) == 2
    assert len(compacted.results[0].tags) == 2


def test_compact_leaves_short_descriptions_alone():
    response = SearchResponse(results=[_summary(description="short")])
    assert results.compact(response).results[0].description == "short"


def test_compact_tolerates_a_missing_description():
    response = SearchResponse(results=[_summary(description=None)])
    assert results.compact(response).results[0].description is None


def test_rest_and_mcp_paths_differ_only_in_verbosity():
    """The REST endpoint keeps the full record; only the MCP path compacts."""
    response = SearchResponse(results=[_summary()])
    assert len(response.results[0].description) == 900
    assert len(results.compact(response).results[0].description) < 900
