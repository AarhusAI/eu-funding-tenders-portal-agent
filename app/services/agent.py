"""PydanticAI agent that answers questions about EU funding call topics.

Side-channel pattern: each tool returns a *truncated preview* to the LLM and
writes the *full* trimmed records into ``deps.full_results``. The ``/search``
endpoint returns ``deps.full_results``, so the descriptions and keyword-match
detail the caller gets never have to pass through the model's token budget.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from pydantic_ai import Agent, RunContext

# OpenAIChatModel is what pydantic-ai 1.x/2.x renamed OpenAIModel to; the older
# name appears throughout the sibling agents and in most online examples.
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from app.config import settings
from app.models import SearchProfile
from app.services import corpus, portal_api
from app.services import profile as profile_service

log = logging.getLogger(__name__)

# @TODO: make is possible to overwrite the system prompt via environment variable?
SYSTEM_PROMPT = """\
You are an assistant that helps callers find EU funding opportunities in the
Funding & Tenders Portal.

The caller has a *search profile* that already restricts which call topics you
can see: framework programmes, submission statuses, and which topic identifiers
count (for example those containing "MISS" or a cluster code like CL2/CL4/CL5).
Results are pre-ranked by a keyword score, and each one tells you the score it
got and which keywords it matched.

Workflow:
1. Detect the input language and respond in the same one.
2. Call search_topics once with the caller's question as `text`. Leave the
   filter arguments alone unless the caller explicitly asks for something the
   profile excludes (a closed call, another programme, a different cluster).
3. Call get_topic only when the caller asks about one specific topic in depth.
4. Stop as soon as you have enough matches. Do not call tools repeatedly when
   the results are already good.

For each topic you present, explain briefly why it matches — reference the
matched keywords and the deadline. Always mention the topic identifier, since
that is what the caller uses to find it on the portal. Do not invent topics,
deadlines or budgets: use only what the tools return.
"""


@dataclass
class AgentDeps:
    """Per-request scratch space shared with every tool the agent calls.

    A fresh instance is created for each ``/search`` request and passed in as
    the agent's ``deps``. Tools read/write it via ``ctx.deps`` — this is the
    side-channel where full topic records and counters accumulate.
    """

    # The resolved profile (caller's fields layered over the README default).
    profile: SearchProfile
    # default_factory=list gives each AgentDeps its own list. Writing "= []"
    # would share one list across all instances (a common Python pitfall).
    full_results: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # @TODO: this would be nice to have configurable through environment variable?
    max_results: int = 10
    iterations: int = 0  # how many tools the agent called
    total_matched: int = 0  # matches before max_results truncation
    language_hint: str | None = None


# The model and agent are expensive to build, so we build them once and cache
# them here, reusing the same agent for every request (lazy singleton).
_model: OpenAIChatModel | None = None
_agent: Agent[AgentDeps, str] | None = None

# How much description the LLM sees per topic. The full text goes to the caller
# via deps.full_results regardless; this only bounds the model's token budget.
# @TODO: this would be nice to have configurable through environment variable?
_PREVIEW_CHARS = 300


def _preview(topic: dict[str, Any]) -> dict[str, Any]:
    """The small projection of a topic handed to the LLM."""
    description = topic.get("description") or ""
    return {
        "identifier": topic.get("identifier"),
        "title": topic.get("title"),
        "status": profile_service.STATUS_NAMES.get(str(topic.get("status")), topic.get("status")),
        "deadline": topic.get("deadline_date"),
        "score": topic.get("score", 0),
        "matched_keywords": topic.get("matched_keywords", []),
        "preview": description[:_PREVIEW_CHARS],
    }


def _build_agent() -> Agent[AgentDeps, str]:
    """Build the PydanticAI agent and its tools once, then return the cached instance."""
    global _model, _agent
    if _agent is not None:  # already built on a previous call → reuse it
        return _agent
    _model = OpenAIChatModel(
        settings.agent_model,
        provider=OpenAIProvider(
            base_url=settings.agent_api_base_url,
            api_key=settings.agent_api_key,
        ),
    )
    _agent = Agent(
        _model,
        deps_type=AgentDeps,
        system_prompt=SYSTEM_PROMPT,
    )

    # The @_agent.tool decorator registers each nested async function as a tool
    # the LLM may decide to call. The first argument, ``ctx``, is supplied by
    # the framework and exposes the per-request AgentDeps via ``ctx.deps``.
    @_agent.tool
    async def search_topics(
        ctx: RunContext[AgentDeps],
        text: str | None = None,
        programmes: list[str] | None = None,
        statuses: list[str] | None = None,
        clusters: list[str] | None = None,
        topic_contains: list[str] | None = None,
    ) -> list[dict]:
        """Search EU funding call topics within the caller's profile.

        Args:
            text: Words to match in the topic title, keywords, tags or description.
            programmes: Override the profile's programmes, e.g. ["Horizon Europe"].
            statuses: Override the statuses, e.g. ["Closed"] to look at past calls.
            clusters: Override the cluster codes, e.g. ["CL5"].
            topic_contains: Override the identifier substrings, e.g. ["MISS"].
        """
        ctx.deps.iterations += 1
        # Only the arguments the model actually set override the request profile.
        overrides = SearchProfile(
            programmes=programmes,
            statuses=statuses,
            clusters=clusters,
            topic_contains=topic_contains,
        )
        active = overrides.merged_over(ctx.deps.profile)
        query, warnings = profile_service.server_query(active)
        for warning in warnings:
            if warning not in ctx.deps.warnings:
                ctx.deps.warnings.append(warning)

        languages = profile_service.resolve_languages(active)
        topics = await corpus.get_topics(query, languages)
        ranked = profile_service.apply(topics, active)
        if text:
            ranked = _filter_text(ranked, text)
        ctx.deps.total_matched = len(ranked)

        selected = ranked[: ctx.deps.max_results]
        # Side-channel: stash the FULL records for the endpoint to return...
        ctx.deps.full_results = selected
        # ...and hand the LLM only small previews, to save its token budget.
        return [_preview(topic) for topic in selected]

    @_agent.tool
    async def get_topic(ctx: RunContext[AgentDeps], identifier: str) -> dict:
        """Fetch one call topic in full by its exact identifier.

        Args:
            identifier: A topic code such as "HORIZON-CL5-2027-01-D1-10".
        """
        ctx.deps.iterations += 1
        languages = profile_service.resolve_languages(ctx.deps.profile)
        row = await portal_api.get_by_identifier(identifier, languages=languages)
        if row is None:
            return {"error": f"no call topic found with identifier {identifier!r}"}
        topic = corpus.trim(row)
        total, matched = profile_service.score(topic, ctx.deps.profile.keywords)
        topic = {**topic, "score": total, "matched_keywords": matched}
        # Replace any earlier preview of the same topic, then append.
        ctx.deps.full_results = [
            existing
            for existing in ctx.deps.full_results
            if existing.get("identifier") != topic.get("identifier")
        ]
        ctx.deps.full_results.append(topic)
        return _preview(topic)

    return _agent


def _filter_text(topics: list[dict[str, Any]], text: str) -> list[dict[str, Any]]:
    """Keep topics matching every word of ``text`` somewhere in their content.

    In-memory rather than server-side: the corpus is already loaded, and the
    Portal's own free-text parameter would bypass the cache and reorder results
    by its relevance score, discarding the profile's ranking.
    """
    words = [word for word in text.lower().split() if len(word) > 2]
    if not words:
        return topics
    matched: list[dict[str, Any]] = []
    for topic in topics:
        haystack = " ".join(
            [
                str(topic.get("title") or ""),
                str(topic.get("description") or ""),
                " ".join(topic.get("keywords") or []),
                " ".join(topic.get("tags") or []),
                str(topic.get("identifier") or ""),
            ]
        ).lower()
        if all(word in haystack for word in words):
            matched.append(topic)
    # An over-specific question shouldn't return nothing when the profile did
    # match topics — fall back to the unfiltered ranking rather than an empty list.
    return matched or topics


async def handle(
    query: str,
    max_results: int,
    language: str | None,
    search_profile: SearchProfile | None = None,
) -> AgentDeps:
    """Run the agent loop once and return its deps (full_results + counters).

    Public entry point called by the /search route. Runs under a timeout and
    degrades gracefully: on timeout or any error it logs and returns whatever
    ``deps`` managed to collect so far, rather than failing the request. That
    matters more here than usual — a cold corpus fetch is 10 sequential Portal
    requests, so the first run after a restart is the slow one.
    """
    resolved = profile_service.resolve(search_profile)
    deps = AgentDeps(profile=resolved, max_results=max_results, language_hint=language)
    agent = _build_agent()
    # If the caller declared a language, prepend a hint so the LLM answers in it.
    user_message = query if not language else f"[language={language}] {query}"
    try:
        # wait_for cancels the agent run if it exceeds agent_timeout seconds.
        await asyncio.wait_for(
            agent.run(user_message, deps=deps),
            timeout=settings.agent_timeout,
        )
    except TimeoutError:
        log.warning(
            "agent loop timed out after %ds — returning partial results", settings.agent_timeout
        )
    except Exception:
        log.exception("agent loop failed — returning whatever was gathered")
    return deps
