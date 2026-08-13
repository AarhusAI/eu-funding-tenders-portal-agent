import pytest
from pydantic import ValidationError

from app.models import SearchProfile, SearchRequest


def test_only_set_fields_override_the_base_profile():
    base = SearchProfile(programmes=["Horizon Europe"], clusters=["CL2"], keywords=["Cloud"])
    merged = SearchProfile(clusters=["CL5"]).merged_over(base)
    assert merged.clusters == ["CL5"]
    assert merged.programmes == ["Horizon Europe"]  # untouched
    assert merged.keywords == ["Cloud"]


def test_an_empty_override_changes_nothing():
    base = SearchProfile(clusters=["CL2"])
    assert SearchProfile().merged_over(base).clusters == ["CL2"]


def test_an_explicit_empty_list_does_override():
    """[] is a real choice ("no cluster filter"), distinct from "unset"."""
    base = SearchProfile(clusters=["CL2"])
    assert SearchProfile(clusters=[]).merged_over(base).clusters == []


def test_search_request_defaults():
    request = SearchRequest(query="funding for smart cities")
    assert request.max_results == 10
    assert request.profile is None
    assert request.language is None


@pytest.mark.parametrize(
    "bad", [{"query": ""}, {"query": "x", "max_results": 0}, {"query": "x", "max_results": 51}]
)
def test_search_request_rejects_out_of_range_values(bad):
    with pytest.raises(ValidationError):
        SearchRequest(**bad)


def test_language_hint_must_be_iso_639_1():
    assert SearchRequest(query="x", language="da").language == "da"
    with pytest.raises(ValidationError):
        SearchRequest(query="x", language="danish")


def test_profile_accepts_a_partial_body():
    request = SearchRequest(query="x", profile={"clusters": ["CL5"]})
    assert request.profile.clusters == ["CL5"]
    assert request.profile.keywords is None  # unset, filled in by profile.resolve
