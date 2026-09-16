"""MCP (Model Context Protocol) server exposing the topic search over Streamable HTTP.

Built with the official MCP SDK's ``MCPServer`` (the class ``FastMCP`` became in
SDK 2.0 — not the standalone ``fastmcp`` package). A single natural-language tool
runs the same agent loop as ``/search``. The server is mounted into the FastAPI
app in ``main.py`` (see the wiring notes there: the session manager must be run
in the app lifespan, and the endpoint is served at ``POST /mcp``).

Note for anyone reading SDK 1.x examples: the host allowlist used to be a
constructor argument. In 2.0 it is passed to ``streamable_http_app()`` instead,
which is where ``main.py`` supplies it.
"""

from mcp.server import MCPServer

from app.config import settings
from app.models import SearchProfile
from app.services.results import compact, run_search

mcp = MCPServer("eu-funding-tenders-portal-agent")


@mcp.tool()
async def search_funding_topics(
    query: str,
    max_results: int = settings.agent_max_results,
    language: str | None = None,
    programmes: list[str] | None = None,
    statuses: list[str] | None = None,
    clusters: list[str] | None = None,
    topic_contains: list[str] | None = None,
    keywords: list[str] | None = None,
) -> str:
    """Search EU Funding & Tenders Portal call topics in natural language.

    Returns call topics matching a search profile, ranked by how many of the
    profile's keywords they match. Unset filters fall back to the configured
    default profile (Horizon Europe + Digital Europe, Forthcoming and Open for
    submission, topics in clusters CL2/CL4/CL5 or containing "MISS").

    Args:
        query: A natural-language description of the funding topics to find.
        max_results: Maximum number of topics to return; out-of-range values are
            clamped to the configured bounds rather than rejected.
        language: Optional ISO-639-1 hint for the reply language, e.g. "da".
        programmes: Framework programmes, e.g. ["Horizon Europe"].
        statuses: Submission statuses, e.g. ["Forthcoming", "Open for submission"].
        clusters: Cluster codes to match in the topic identifier, e.g. ["CL5"].
        topic_contains: Substrings the topic identifier must contain, e.g. ["MISS"].
        keywords: Keywords that additively boost a topic's ranking score.

    Returns:
        A JSON object matching SearchResponse: {"results": [...],
        "total_matched": int, "profile_used": {...}, "iterations": int}.
    """
    # Clamp to the same bounds SearchRequest enforces for the REST endpoint.
    # Imperative here because MCP tool args bypass SearchRequest's validation.
    max_results = max(1, min(max_results, settings.agent_max_results_cap))
    # Only fields the caller actually set become overrides; the rest fall back
    # to the default profile inside run_search.
    overrides = SearchProfile(
        programmes=programmes,
        statuses=statuses,
        clusters=clusters,
        topic_contains=topic_contains,
        keywords=keywords,
    )
    has_overrides = bool(overrides.model_dump(exclude_none=True))
    response = await run_search(
        query=query,
        max_results=max_results,
        language=language,
        search_profile=overrides if has_overrides else None,
    )
    # Compact before returning: this payload is replayed into the model's context
    # on every chat turn, so full descriptions would overflow it.
    return compact(response).model_dump_json()
