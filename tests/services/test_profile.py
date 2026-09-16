"""The pure filter/rank rules — no network, no LLM, no clock.

Several of these encode findings that cost real probing time: the MISS rule has
to match mid-identifier, description hits must score below metadata hits, and
identifier filters are OR-ed rather than AND-ed.
"""

from app.models import SearchProfile
from app.services import profile as profile_service

# --- server-side query -------------------------------------------------------


def test_server_query_matches_the_verified_body():
    """This exact body is what returned 970 English topics against production."""
    query, warnings = profile_service.server_query(profile_service.DEFAULT_PROFILE)
    assert query == {
        "bool": {
            "must": [
                {"terms": {"type": ["1"]}},
                {"terms": {"status": ["31094501", "31094502"]}},
                {"terms": {"frameworkProgramme": ["43108390", "43152860"]}},
            ]
        }
    }
    assert warnings == []


def test_server_query_omits_absent_filters():
    query, warnings = profile_service.server_query(SearchProfile())
    assert query == {"bool": {"must": [{"terms": {"type": ["1"]}}]}}
    assert warnings == []


def test_unknown_names_are_reported_not_silently_dropped():
    """A typo'd programme would otherwise widen the search while looking narrower."""
    query, warnings = profile_service.server_query(
        SearchProfile(programmes=["Horizon Europe", "Horizen Europe"], statuses=["Nope"])
    )
    assert query["bool"]["must"][-1] == {"terms": {"frameworkProgramme": ["43108390"]}}
    assert any("Horizen Europe" in w for w in warnings)
    assert any("Nope" in w for w in warnings)


def test_programme_and_status_names_are_case_insensitive():
    query, warnings = profile_service.server_query(
        SearchProfile(programmes=["  horizon europe "], statuses=["FORTHCOMING"])
    )
    assert warnings == []
    assert {"terms": {"frameworkProgramme": ["43108390"]}} in query["bool"]["must"]
    assert {"terms": {"status": ["31094501"]}} in query["bool"]["must"]


def test_identifier_query_pins_type_to_call_topic():
    """Unpinned, several unrelated documents share one identifier — one lookup
    returned 5 rows whose first was a stub."""
    query = profile_service.identifier_query("HORIZON-CL5-2026-09-D2-01")
    assert {"terms": {"type": ["1"]}} in query["bool"]["must"]
    assert {"terms": {"identifier": ["HORIZON-CL5-2026-09-D2-01"]}} in query["bool"]["must"]


# --- profile resolution ------------------------------------------------------


def test_resolve_uses_the_readme_default_when_unset():
    assert profile_service.resolve(None) is profile_service.DEFAULT_PROFILE


def test_partial_profile_keeps_the_defaults_it_did_not_set():
    resolved = profile_service.resolve(SearchProfile(clusters=["CL5"]))
    assert resolved.clusters == ["CL5"]
    assert resolved.statuses == ["Forthcoming", "Open for submission"]
    assert resolved.keywords == profile_service.DEFAULT_KEYWORDS


def test_resolve_languages_prefers_the_profile_over_the_env_default():
    assert profile_service.resolve_languages(SearchProfile()) == ["en"]
    assert profile_service.resolve_languages(SearchProfile(languages=["da", "en"])) == ["da", "en"]


# --- keyword scoring ---------------------------------------------------------


def test_metadata_hit_outscores_description_only_hit(topics, identifiers):
    """The 26-vs-44 split: "Cloud" is in 26 topics' metadata but 44 more only in
    the description, so weighting them equally would let prose outrank relevance."""
    by_id = {t["identifier"]: t for t in topics}
    metadata_hit = by_id[identifiers["cl2"]]
    description_hit = by_id[identifiers["cl4"]]

    assert profile_service.score(metadata_hit, ["Research"]) == (2, ["Research"])
    assert profile_service.score(description_hit, ["Innovation"]) == (1, ["Innovation"])


def test_score_is_additive_over_distinct_keywords(topics, identifiers):
    topic = {t["identifier"]: t for t in topics}[identifiers["cl2"]]
    total, matched = profile_service.score(topic, ["Research", "Research", "research"])
    assert matched == ["Research"]  # case-insensitive dedupe, first spelling kept
    assert total == 2


def test_word_boundaries_stop_substring_false_positives():
    topic = {"title": "A clouded judgement", "description": "cloudy"}
    assert profile_service.score(topic, ["Cloud"]) == (0, [])
    assert profile_service.score({"title": "Cloud native"}, ["Cloud"]) == (2, ["Cloud"])


def test_keywords_with_punctuation_match_literally():
    """ "TRL 4-7" and "Web 4.0" must not behave as regex metacharacters."""
    topic = {"title": "Pilots at TRL 4-7 using Web 4.0", "description": ""}
    total, matched = profile_service.score(topic, ["TRL 4-7", "Web 4.0"])
    assert sorted(matched) == ["TRL 4-7", "Web 4.0"]
    assert total == 4
    # The "." in "Web 4.0" is literal, so "Web 410" must not match.
    assert profile_service.score({"title": "Web 410"}, ["Web 4.0"]) == (0, [])


def test_keyword_bounded_by_non_word_characters():
    """A keyword ending in punctuation gets no trailing \\b — with one it could
    never match, since \\b requires an adjacent word character."""
    topic = {"title": "Built in C++ mostly", "description": ""}
    assert profile_service.score(topic, ["C++"]) == (2, ["C++"])


def test_blank_and_missing_keywords_are_ignored():
    assert profile_service.score({"title": "x"}, None) == (0, [])
    assert profile_service.score({"title": "x"}, ["", "   "]) == (0, [])


def test_score_reads_keywords_and_tags_as_metadata():
    topic = {"title": "", "keywords": ["Dataspace"], "tags": ["mobility"], "description": ""}
    total, matched = profile_service.score(topic, ["Dataspace", "Mobility"])
    assert total == 4
    assert sorted(matched) == ["Dataspace", "Mobility"]


# --- identifier filtering ----------------------------------------------------


def test_miss_matches_mid_identifier_not_just_a_prefix():
    """MISS appears both as "HORIZON-MISS-..." and inside longer codes such as
    HORIZON-HLTH-2027-01-ENVHLTH-MISSCLIMA-03."""
    profile = SearchProfile(topic_contains=["MISS"], clusters=[], keywords=[])
    topics = [
        {"identifier": "HORIZON-MISS-2023-CLIMA-01-01"},
        {"identifier": "HORIZON-HLTH-2027-01-ENVHLTH-MISSCLIMA-03"},
        {"identifier": "HORIZON-CL6-2026-04-GOVERNANCE-01"},
    ]
    kept = [t["identifier"] for t in profile_service.apply(topics, profile)]
    assert kept == [
        "HORIZON-HLTH-2027-01-ENVHLTH-MISSCLIMA-03",
        "HORIZON-MISS-2023-CLIMA-01-01",
    ]


def test_topic_contains_and_clusters_are_or_ed(topics, identifiers):
    """The README asks for MISS *or* cluster 2/4/5 — reading it as AND would
    return almost nothing."""
    kept = {
        t["identifier"] for t in profile_service.apply(topics, profile_service.DEFAULT_PROFILE)
    }
    assert identifiers["miss"] in kept  # MISS but not a CL2/4/5 cluster
    assert identifiers["cl2"] in kept
    assert identifiers["cl4"] in kept
    assert identifiers["cl5"] in kept
    assert identifiers["other"] not in kept  # CL6 matches neither rule


def test_no_identifier_filters_keeps_everything(topics):
    profile = SearchProfile(topic_contains=[], clusters=[], keywords=[])
    assert len(profile_service.apply(topics, profile)) == len(topics)


def test_ranking_orders_by_score_then_deadline_then_identifier():
    profile = SearchProfile(topic_contains=[], clusters=[], keywords=["Cloud"])
    topics = [
        {"identifier": "B", "title": "plain", "deadline_date": "2026-01-01"},
        {"identifier": "A", "title": "plain", "deadline_date": "2026-01-01"},
        {"identifier": "C", "title": "Cloud", "deadline_date": "2027-01-01"},
        {"identifier": "D", "title": "plain"},  # no deadline → last among equals
    ]
    assert [t["identifier"] for t in profile_service.apply(topics, profile)] == [
        "C",
        "A",
        "B",
        "D",
    ]


def test_apply_does_not_mutate_its_input():
    topics = [{"identifier": "HORIZON-CL5-1", "title": "Cloud"}]
    profile_service.apply(topics, SearchProfile(keywords=["Cloud"], clusters=["CL5"]))
    assert "score" not in topics[0]


def test_env_overrides_extend_the_builtin_programme_table():
    """A newly published programme should be an env var, not a release."""
    merged = profile_service._with_overrides(
        profile_service._BUILTIN_PROGRAMME_IDS, {"Creative Europe": "99999999"}
    )
    assert merged["creative europe"] == "99999999"
    assert merged["horizon europe"] == "43108390"  # built-ins survive


def test_an_env_override_can_correct_a_builtin_id():
    merged = profile_service._with_overrides(
        profile_service._BUILTIN_STATUS_IDS, {"Closed": "31094599"}
    )
    assert merged["closed"] == "31094599"


def test_override_keys_are_lowercased_so_they_replace_rather_than_duplicate():
    """Lookup is case-insensitive; an unnormalised key would shadow nothing."""
    merged = profile_service._with_overrides({"horizon europe": "1"}, {"  HORIZON EUROPE  ": "2"})
    assert merged == {"horizon europe": "2"}
