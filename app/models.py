from pydantic import BaseModel, Field

from app.config import settings

# Pydantic models double as the API's request/response schema: FastAPI uses
# them to validate incoming JSON and to serialise outgoing JSON. A "str | None"
# type means the field is optional and may be null.


class SearchProfile(BaseModel):
    """A search profile: what to retrieve, what to keep, and what to rank higher.

    Every field is optional; an unset field falls back to the corresponding
    default in ``app.services.profile.DEFAULT_PROFILE`` (which encodes the
    example from the README). Two fields are pushed to the Portal as server-side
    filters (``programmes``, ``statuses``, plus ``languages``); the rest are
    applied client-side, because the Portal's query DSL accepts only
    ``bool``/``must``/``terms`` and rejects any substring match on an identifier.
    """

    programmes: list[str] | None = Field(
        default=None,
        description='Framework programme names, e.g. ["Horizon Europe", "Digital Europe"].',
    )
    statuses: list[str] | None = Field(
        default=None,
        description='Submission statuses, e.g. ["Forthcoming", "Open for submission"].',
    )
    topic_contains: list[str] | None = Field(
        default=None,
        description='Substrings that must appear in the topic identifier, e.g. ["MISS"].',
    )
    clusters: list[str] | None = Field(
        default=None,
        description='Cluster codes matched in the topic identifier, e.g. ["CL2","CL4","CL5"].',
    )
    keywords: list[str] | None = Field(
        default=None,
        description="Keywords that additively boost a topic's ranking score.",
    )
    # Distinct from SearchRequest.language: this selects which language's copy of
    # each topic is *retrieved* from the Portal, whereas SearchRequest.language
    # is a hint for which language the agent should *answer* in.
    languages: list[str] | None = Field(
        default=None,
        description=(
            "ISO-639-1 codes selecting which language copy of each topic to retrieve, "
            'in preference order with per-topic fallback, e.g. ["da","en"]. '
            "Defaults to the PORTAL_LANGUAGES setting."
        ),
    )

    def merged_over(self, base: "SearchProfile") -> "SearchProfile":
        """Return a copy of ``base`` with this profile's set fields layered on top.

        Used for per-request profiles (fall back to the default profile) and for
        the agent's per-tool-call overrides (fall back to the request profile).
        Only fields explicitly set on ``self`` win, so a caller sending
        ``{"clusters": ["CL5"]}`` keeps the default keywords and statuses.
        """
        overrides = self.model_dump(exclude_none=True)
        return base.model_copy(update=overrides)


class SearchRequest(BaseModel):
    """Incoming body for ``POST /search`` (validated by FastAPI)."""

    # Field(...) attaches validation rules; a bad value is rejected with a 422.
    query: str = Field(min_length=1, max_length=2000)  # non-empty, capped length
    profile: SearchProfile | None = None  # unset → the README's default profile
    # Bounds come from settings, so /search and the MCP tool clamp identically.
    # Unlike every other settings read in this codebase these are evaluated at
    # import time — a Field default has to be — which is fine because Settings()
    # is itself built at import, but it does mean tests monkeypatching
    # agent_max_results won't move this default.
    max_results: int = Field(  # ge/le = inclusive min/max
        default=settings.agent_max_results,
        ge=1,
        le=settings.agent_max_results_cap,
    )
    language: str | None = Field(default=None, pattern=r"^[a-z]{2}$")  # reply language hint


class TopicSummary(BaseModel):
    """A single call topic as returned to the caller."""

    identifier: str  # e.g. "HORIZON-CL5-2027-01-D1-10"
    title: str
    status: str | None = None  # human-readable, e.g. "Open for submission"
    deadline_date: str | None = None
    start_date: str | None = None
    call_identifier: str | None = None
    programme: str | None = None  # human-readable, e.g. "Horizon Europe"
    url: str | None = None
    description: str | None = None
    language: str | None = None  # which language copy this record came from
    keywords: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    # Why this topic ranked where it did — returned so the LLM (and the caller)
    # can explain the ordering rather than presenting it as a black box.
    score: int = 0
    matched_keywords: list[str] = Field(default_factory=list)
    rationale: str | None = None  # the agent's own explanation, if it gave one


class SearchResponse(BaseModel):
    """Outgoing body for ``POST /search``."""

    results: list[TopicSummary]
    # How many topics matched the profile in total, before max_results truncation.
    total_matched: int = 0
    profile_used: SearchProfile | None = None
    query_language_detected: str | None = None
    iterations: int = 0  # how many tool-calling rounds the agent used
    # Set when the profile named a programme/status the Portal doesn't know, so a
    # typo surfaces to the caller instead of silently narrowing the search.
    warnings: list[str] = Field(default_factory=list)
