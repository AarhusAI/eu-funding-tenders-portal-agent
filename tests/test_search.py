"""End-to-end: POST /search -> agent (scripted LLM) -> Portal (mocked HTTP) ->
filtered, ranked, deduped response."""

import httpx
import respx
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from app.services import agent as agent_module
from tests.conftest import PORTAL_URL


def _script(tool_args: dict):
    def gen(messages, info):
        called = any(
            getattr(part, "part_kind", None) == "tool-return"
            for message in messages
            for part in message.parts
        )
        if called:
            return ModelResponse(parts=[TextPart(content="Here are the matching topics.")])
        return ModelResponse(parts=[ToolCallPart(tool_name="search_topics", args=tool_args)])

    return FunctionModel(gen)


@respx.mock
async def test_search_applies_the_readme_profile(client, api_headers, portal_page, identifiers):
    respx.post(PORTAL_URL).mock(return_value=httpx.Response(200, json=portal_page))
    built = agent_module._build_agent()
    with built.override(model=_script({})):
        response = await client.post(
            "/search", json={"query": "smart city IoT dataspace"}, headers=api_headers
        )

    assert response.status_code == 200
    body = response.json()
    returned = [r["identifier"] for r in body["results"]]

    # Only MISS or CL2/CL4/CL5 topics survive; CL6 and CL3 do not.
    assert identifiers["miss"] in returned
    assert identifiers["other"] not in returned
    assert identifiers["desc_only"] not in returned
    # No identifier appears twice, even though the fixture contains a real dupe.
    assert len(returned) == len(set(returned))
    assert identifiers["duplicate"] not in returned

    # Ranking metadata is exposed so the ordering can be explained.
    assert body["results"] == sorted(body["results"], key=lambda r: -r["score"])
    top = body["results"][0]
    assert top["matched_keywords"]
    assert top["status"] in {"Forthcoming", "Open for submission"}
    assert top["programme"] == "Horizon Europe"
    assert body["profile_used"]["clusters"] == ["CL2", "CL4", "CL5"]
    assert body["iterations"] == 1
    assert body["warnings"] == []


@respx.mock
async def test_caller_profile_narrows_the_search(client, api_headers, portal_page, identifiers):
    respx.post(PORTAL_URL).mock(return_value=httpx.Response(200, json=portal_page))
    built = agent_module._build_agent()
    with built.override(model=_script({})):
        response = await client.post(
            "/search",
            json={
                "query": "cluster five only",
                "profile": {"clusters": ["CL5"], "topic_contains": []},
            },
            headers=api_headers,
        )

    body = response.json()
    assert [r["identifier"] for r in body["results"]] == [identifiers["cl5"]]
    # Unset fields still come from the README default.
    assert body["profile_used"]["statuses"] == ["Forthcoming", "Open for submission"]


@respx.mock
async def test_max_results_pages_without_hiding_the_match_count(client, api_headers, portal_page):
    respx.post(PORTAL_URL).mock(return_value=httpx.Response(200, json=portal_page))
    built = agent_module._build_agent()
    with built.override(model=_script({})):
        response = await client.post(
            "/search", json={"query": "funding", "max_results": 2}, headers=api_headers
        )

    body = response.json()
    assert len(body["results"]) == 2
    assert body["total_matched"] > 2


@respx.mock
async def test_an_unknown_programme_is_reported_to_the_caller(client, api_headers, portal_page):
    respx.post(PORTAL_URL).mock(return_value=httpx.Response(200, json=portal_page))
    built = agent_module._build_agent()
    with built.override(model=_script({})):
        response = await client.post(
            "/search",
            json={"query": "typo", "profile": {"programmes": ["Horizen Europe"]}},
            headers=api_headers,
        )
    assert any("Horizen Europe" in w for w in response.json()["warnings"])


async def test_malformed_body_is_rejected(client, api_headers):
    response = await client.post("/search", json={"query": ""}, headers=api_headers)
    assert response.status_code == 422
