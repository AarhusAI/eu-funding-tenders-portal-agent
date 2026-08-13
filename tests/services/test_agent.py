"""The agent's tools and side-channel pattern, without a real LLM.

FunctionModel scripts an exact tool call so the assertions are deterministic;
TestModel exercises the tool with library-generated arguments. No HTTP request
ever reaches an LLM.
"""

import asyncio

import httpx
import respx
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from app.models import SearchProfile
from app.services import agent as agent_module
from tests.conftest import PORTAL_URL


def _script_tool(tool_name: str, tool_args: dict):
    """A FunctionModel that calls one tool once, then answers with text."""

    def gen(messages, info):
        already_called = any(
            getattr(part, "part_kind", None) == "tool-return"
            for message in messages
            for part in message.parts
        )
        if already_called:
            return ModelResponse(parts=[TextPart(content="done")])
        return ModelResponse(parts=[ToolCallPart(tool_name=tool_name, args=tool_args)])

    return FunctionModel(gen)


@respx.mock
async def test_search_tool_keeps_full_records_out_of_the_llms_budget(portal_page, identifiers):
    """The side channel: full records to deps, truncated previews to the model."""
    respx.post(PORTAL_URL).mock(return_value=httpx.Response(200, json=portal_page))
    built = agent_module._build_agent()
    with built.override(model=_script_tool("search_topics", {})):
        deps = await agent_module.handle(query="funding", max_results=10, language=None)

    kept = {t["identifier"] for t in deps.full_results}
    assert identifiers["miss"] in kept
    assert identifiers["other"] not in kept  # CL6 matches neither profile rule
    # The full description survives in the side channel; the preview is capped.
    longest = max(deps.full_results, key=lambda t: len(t["description"]))
    assert len(longest["description"]) > agent_module._PREVIEW_CHARS
    assert len(agent_module._preview(longest)["preview"]) == agent_module._PREVIEW_CHARS
    assert deps.iterations == 1
    assert deps.total_matched >= len(deps.full_results)


@respx.mock
async def test_tool_arguments_override_the_request_profile(portal_page, identifiers):
    respx.post(PORTAL_URL).mock(return_value=httpx.Response(200, json=portal_page))
    built = agent_module._build_agent()
    with built.override(
        model=_script_tool("search_topics", {"clusters": ["CL5"], "topic_contains": []})
    ):
        deps = await agent_module.handle(query="cluster 5 only", max_results=10, language=None)

    kept = {t["identifier"] for t in deps.full_results}
    assert kept == {identifiers["cl5"]}


@respx.mock
async def test_text_argument_narrows_the_ranked_results(portal_page, identifiers):
    """Text matching runs in memory over the cached corpus rather than via the
    Portal's own free-text parameter, which would reorder by its own relevance
    score and discard the profile's ranking."""
    respx.post(PORTAL_URL).mock(return_value=httpx.Response(200, json=portal_page))
    built = agent_module._build_agent()
    with built.override(model=_script_tool("search_topics", {"text": identifiers["cl5"]})):
        deps = await agent_module.handle(query="that one", max_results=10, language=None)

    assert [t["identifier"] for t in deps.full_results] == [identifiers["cl5"]]
    assert deps.total_matched == 1


@respx.mock
async def test_unknown_filter_values_surface_as_warnings(portal_page):
    respx.post(PORTAL_URL).mock(return_value=httpx.Response(200, json=portal_page))
    built = agent_module._build_agent()
    with built.override(model=_script_tool("search_topics", {"programmes": ["Horizen Europe"]})):
        deps = await agent_module.handle(query="typo", max_results=5, language=None)
    assert any("Horizen Europe" in w for w in deps.warnings)


@respx.mock
async def test_max_results_bounds_the_page_but_not_the_match_count(portal_page):
    respx.post(PORTAL_URL).mock(return_value=httpx.Response(200, json=portal_page))
    built = agent_module._build_agent()
    with built.override(model=_script_tool("search_topics", {})):
        deps = await agent_module.handle(query="funding", max_results=1, language=None)
    assert len(deps.full_results) == 1
    assert deps.total_matched > 1  # "1 of N", not "1 of 1"


@respx.mock
async def test_get_topic_fetches_one_topic_live(portal_page, identifiers):
    """topicDetails/<id>.json is dead (404), so depth comes from a terms lookup."""
    row = next(
        r for r in portal_page["results"] if r["metadata"]["identifier"][0] == identifiers["cl5"]
    )
    respx.post(PORTAL_URL).mock(return_value=httpx.Response(200, json={"results": [row]}))
    built = agent_module._build_agent()
    with built.override(model=_script_tool("get_topic", {"identifier": identifiers["cl5"]})):
        deps = await agent_module.handle(query="tell me about it", max_results=5, language=None)

    assert [t["identifier"] for t in deps.full_results] == [identifiers["cl5"]]
    assert "score" in deps.full_results[0]


@respx.mock
async def test_get_topic_reports_a_missing_identifier():
    respx.post(PORTAL_URL).mock(return_value=httpx.Response(200, json={"results": []}))
    built = agent_module._build_agent()
    with built.override(model=_script_tool("get_topic", {"identifier": "NOPE-1"})):
        deps = await agent_module.handle(query="missing", max_results=5, language=None)
    assert deps.full_results == []


@respx.mock
async def test_get_topic_replaces_an_earlier_copy_of_the_same_topic(portal_page, identifiers):
    row = next(
        r for r in portal_page["results"] if r["metadata"]["identifier"][0] == identifiers["cl5"]
    )
    respx.post(PORTAL_URL).mock(return_value=httpx.Response(200, json={"results": [row]}))
    built = agent_module._build_agent()

    def gen(messages, info):
        calls = sum(
            1
            for message in messages
            for part in message.parts
            if getattr(part, "part_kind", None) == "tool-return"
        )
        if calls < 2:
            return ModelResponse(
                parts=[
                    ToolCallPart(tool_name="get_topic", args={"identifier": identifiers["cl5"]})
                ]
            )
        return ModelResponse(parts=[TextPart(content="done")])

    with built.override(model=FunctionModel(gen)):
        deps = await agent_module.handle(query="twice", max_results=5, language=None)
    assert len(deps.full_results) == 1  # not appended twice


# --- in-memory text filtering ------------------------------------------------


def test_text_filter_requires_every_significant_word():
    topics = [
        {"title": "Smart city dataspace pilot", "identifier": "A"},
        {"title": "Soil health monitoring", "identifier": "B"},
    ]
    assert [t["identifier"] for t in agent_module._filter_text(topics, "smart dataspace")] == ["A"]


def test_text_filter_ignores_short_words():
    topics = [{"title": "Anything", "identifier": "A"}]
    assert agent_module._filter_text(topics, "of an in") == topics


def test_text_filter_falls_back_rather_than_returning_nothing():
    """An over-specific question shouldn't erase results the profile did match."""
    topics = [{"title": "Soil", "identifier": "A"}]
    assert agent_module._filter_text(topics, "quantum submarine") == topics


# --- degradation -------------------------------------------------------------


async def test_handle_returns_partial_results_on_timeout(monkeypatch):
    """A cold corpus fetch is 10 sequential Portal requests, so the first run
    after a restart is the one most likely to hit this."""
    built = agent_module._build_agent()

    async def slow_run(*args, **kwargs):
        await asyncio.sleep(1)

    monkeypatch.setattr(built, "run", slow_run)
    monkeypatch.setattr(agent_module.settings, "agent_timeout", 0.01)
    deps = await agent_module.handle(query="x", max_results=3, language=None)
    assert deps.full_results == []


async def test_handle_returns_partial_results_on_error(monkeypatch):
    built = agent_module._build_agent()

    async def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(built, "run", boom)
    deps = await agent_module.handle(query="x", max_results=3, language=None)
    assert deps.full_results == []


async def test_language_hint_is_passed_through(monkeypatch):
    built = agent_module._build_agent()
    seen = {}

    async def capture(prompt, **kwargs):
        seen["prompt"] = prompt

    monkeypatch.setattr(built, "run", capture)
    deps = await agent_module.handle(query="hvad er nyt?", max_results=3, language="da")
    assert seen["prompt"].startswith("[language=da]")
    assert deps.language_hint == "da"


def test_agent_is_built_once():
    assert agent_module._build_agent() is agent_module._build_agent()


def test_handle_resolves_a_partial_profile():
    profile = agent_module.profile_service.resolve(SearchProfile(clusters=["CL5"]))
    assert profile.clusters == ["CL5"]
    assert profile.keywords == agent_module.profile_service.DEFAULT_KEYWORDS
