"""The MCP tool reuses the same path as /search but returns compacted JSON."""

import json

import httpx
import respx
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from app import mcp_server
from app.services import agent as agent_module
from tests.conftest import PORTAL_URL


def _script():
    def gen(messages, info):
        called = any(
            getattr(part, "part_kind", None) == "tool-return"
            for message in messages
            for part in message.parts
        )
        if called:
            return ModelResponse(parts=[TextPart(content="done")])
        return ModelResponse(parts=[ToolCallPart(tool_name="search_topics", args={})])

    return FunctionModel(gen)


@respx.mock
async def test_mcp_tool_returns_compacted_json(portal_page, identifiers):
    respx.post(PORTAL_URL).mock(return_value=httpx.Response(200, json=portal_page))
    built = agent_module._build_agent()
    with built.override(model=_script()):
        payload = await mcp_server.search_funding_topics(query="smart city")

    body = json.loads(payload)
    assert identifiers["miss"] in [r["identifier"] for r in body["results"]]
    assert body["total_matched"] >= len(body["results"])
    # Compacted: descriptions are capped for the model's context budget.
    for result in body["results"]:
        assert result["description"] is None or len(result["description"]) <= 401


@respx.mock
async def test_mcp_tool_accepts_profile_overrides(portal_page, identifiers):
    respx.post(PORTAL_URL).mock(return_value=httpx.Response(200, json=portal_page))
    built = agent_module._build_agent()
    with built.override(model=_script()):
        payload = await mcp_server.search_funding_topics(
            query="cluster five", clusters=["CL5"], topic_contains=[]
        )
    body = json.loads(payload)
    assert [r["identifier"] for r in body["results"]] == [identifiers["cl5"]]


@respx.mock
async def test_mcp_tool_clamps_max_results(portal_page):
    respx.post(PORTAL_URL).mock(return_value=httpx.Response(200, json=portal_page))
    built = agent_module._build_agent()
    with built.override(model=_script()):
        payload = await mcp_server.search_funding_topics(query="x", max_results=999)
    assert len(json.loads(payload)["results"]) <= 50

    with built.override(model=_script()):
        payload = await mcp_server.search_funding_topics(query="x", max_results=0)
    assert len(json.loads(payload)["results"]) == 1
