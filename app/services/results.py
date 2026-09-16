"""Shape the agent's raw side-channel results into a ``SearchResponse``.

The agent's tools stash complete trimmed topic records in ``deps.full_results``
(the LLM only ever sees truncated previews — see ``services/agent.py``). This
module turns those into the response both callers return: the ``/search`` HTTP
route (``routes/search.py``) and the MCP ``search_funding_topics`` tool
(``mcp_server.py``). Keeping it here means the shaping logic — and the
load-bearing "return full_results, not what the LLM saw" split — lives in one
place.
"""

from app.config import settings
from app.models import SearchProfile, SearchResponse, TopicSummary
from app.services import agent
from app.services import profile as profile_service


async def run_search(
    query: str,
    max_results: int,
    language: str | None,
    search_profile: SearchProfile | None = None,
) -> SearchResponse:
    """Run the agent loop and shape its collected records into a ``SearchResponse``."""
    deps = await agent.handle(
        query=query,
        max_results=max_results,
        language=language,
        search_profile=search_profile,
    )
    results = deps.full_results[:max_results]
    return SearchResponse(
        results=[_to_summary(topic) for topic in results],
        # total_matched counts everything the profile matched, so a caller can
        # tell "10 of 398" from "10 of 10" — max_results only bounds the page.
        total_matched=max(deps.total_matched, len(results)),
        profile_used=deps.profile,
        query_language_detected=deps.language_hint,
        iterations=deps.iterations,
        warnings=deps.warnings,
    )


def _to_summary(topic: dict) -> TopicSummary:
    """Convert a trimmed topic dict into the ``TopicSummary`` we return.

    Status and programme are translated from the Portal's numeric ids into the
    names a human reads; an unknown id passes through unchanged rather than
    becoming None, so a new Portal code shows up as itself instead of vanishing.
    """
    status = topic.get("status")
    programme = topic.get("framework_programme")
    return TopicSummary(
        identifier=str(topic.get("identifier") or ""),
        title=str(topic.get("title") or ""),
        status=profile_service.STATUS_NAMES.get(str(status), status) if status else None,
        deadline_date=topic.get("deadline_date"),
        start_date=topic.get("start_date"),
        call_identifier=topic.get("call_identifier"),
        programme=(
            profile_service.PROGRAMME_NAMES.get(str(programme), programme) if programme else None
        ),
        url=topic.get("url"),
        description=topic.get("description"),
        language=topic.get("language"),
        keywords=topic.get("keywords") or [],
        tags=topic.get("tags") or [],
        score=int(topic.get("score") or 0),
        matched_keywords=topic.get("matched_keywords") or [],
        rationale=topic.get("rationale"),
    )


def compact(response: SearchResponse) -> SearchResponse:
    """Return a token-lean copy of ``response`` for LLM callers (the MCP tool).

    Topic descriptions run to thousands of characters. A chat client like
    OpenWebUI replays every tool result verbatim in the message history on every
    turn, so an untrimmed payload accumulates across turns and eventually
    overflows the model's context — the follow-up reply then silently fails to
    generate. The REST ``/search`` endpoint still returns the full records; only
    the MCP path compacts.
    """
    return SearchResponse(
        results=[_compact_summary(topic) for topic in response.results],
        total_matched=response.total_matched,
        profile_used=response.profile_used,
        query_language_detected=response.query_language_detected,
        iterations=response.iterations,
        warnings=response.warnings,
    )


def _compact_summary(topic: TopicSummary) -> TopicSummary:
    """Trim one summary: description truncated, keyword and tag lists capped.

    Both bounds are settings (MCP_MAX_DESCRIPTION_CHARS, MCP_MAX_KEYWORDS) and
    are read at call time, so a deployment whose chat client has a roomier
    context can raise them without a rebuild.
    """
    max_chars = settings.mcp_max_description_chars
    max_keywords = settings.mcp_max_keywords
    description = topic.description
    if description and len(description) > max_chars:
        description = description[:max_chars].rstrip() + "…"
    return topic.model_copy(
        update={
            "description": description,
            # The Portal repeats the identifier and call id inside keywords, and
            # the list runs long; the matched ones carry the ranking rationale.
            "keywords": topic.keywords[:max_keywords],
            "tags": topic.tags[:max_keywords],
        }
    )
