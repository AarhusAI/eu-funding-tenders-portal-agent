from typing import Literal

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application configuration, loaded from environment variables.

    pydantic-settings reads the environment (and a local ``.env`` file) the
    moment ``Settings()`` is instantiated below. Any field declared *without*
    a default is required: if it is missing the constructor raises and the
    service fails fast at startup rather than crashing later.
    """

    model_config = {
        "env_file": ".env",  # also read variables from a local .env file
        "env_file_encoding": "utf-8",
        "extra": "ignore",  # silently ignore unknown env vars instead of erroring
    }

    # --- Required (no default → must be set in the environment) ---
    api_key: str  # token clients must send to call this agent

    agent_model: str = "gpt-4o-mini"  # LLM model name passed to LiteLLM
    agent_api_base_url: str = "http://litellm:4000/v1"  # LiteLLM endpoint
    agent_api_key: str  # required: this agent → LiteLLM
    # 60s not 30s: a cold corpus fetch is 10 sequential Portal requests, so the
    # first agent run after a restart is much slower than later ones.
    agent_timeout: int = 60
    # Tool-call budget per query, enforced via PydanticAI's UsageLimits. Backstop
    # for a model that keeps calling search_topics instead of answering; the run
    # ends and the caller gets whatever the tools already collected.
    agent_max_iterations: int = 8
    # Replaces the built-in system prompt wholesale when set. The default prompt
    # carries the tool-calling workflow, so an override that omits it stops the
    # agent calling search_topics at all — see app/services/agent.py.
    agent_system_prompt: str | None = None
    # Default page size for /search and the MCP tool, and the hard ceiling both
    # clamp to. The ceiling exists because every returned topic carries a full
    # description; 50 of them is already a large response body.
    agent_max_results: int = 10
    agent_max_results_cap: int = 50
    # How much of each description the LLM sees per topic. The full text still
    # reaches the caller through the deps.full_results side channel, so this
    # only bounds the model's token budget.
    agent_preview_chars: int = 300

    # --- MCP response compaction -------------------------------------------
    # A chat client replays every tool result into the model's context on each
    # turn, so the MCP path trims what /search returns in full.
    mcp_max_description_chars: int = 400
    mcp_max_keywords: int = 10

    # --- EU Funding & Tenders Portal (SEDIA search API) ---
    # Public endpoint; "SEDIA" is the literal apiKey the portal frontend uses,
    # so no real credential is involved.
    portal_search_url: str = "https://api.tech.ec.europa.eu/search-api/prod/rest/search"
    portal_api_key: str = "SEDIA"
    # Extra (or replacement) name → Portal id mappings, merged over the verified
    # built-ins in app/services/profile.py. The built-ins are the Portal's own
    # taxonomy ids and change rarely, but this means a newly published programme
    # is an env var rather than a release. Keys are matched case-insensitively.
    portal_programme_ids: dict[str, str] = {}
    portal_status_ids: dict[str, str] = {}
    # Which language's copy of each topic to retrieve. The index stores every
    # topic in up to 24 languages; coverage varies widely (en 970 topics vs da
    # 536 for the default profile) and the copies are not real translations, so
    # "en" is the sensible default. A list is a preference order with per-topic
    # fallback — see app/services/corpus.py.
    portal_languages: list[str] = ["en"]
    portal_page_size: int = 100
    # Safety cap on pagination: the default profile needs 10 pages, this stops a
    # widened profile from fetching indefinitely.
    portal_max_pages: int = 20
    portal_timeout: int = 30
    portal_cache_ttl: int = 21600  # 6h; topics change daily at most

    # --- Topic-corpus cache -------------------------------------------------
    # Field names mirror AarhusAI/search-agent (minus its SEARCH_AGENT_ env
    # prefix) so the two agents are configured the same way.
    #
    # "memory" keeps the corpus in this process's heap: no infrastructure, but
    # it is per-worker, so a restart re-pays the ~10-20s cold fetch and two
    # workers hold two copies. "redis" shares one copy across instances and
    # survives restarts. "disabled" re-fetches on every search (debugging only).
    # A Redis outage degrades to slow searches, never failed ones — the backend
    # fails open, see app/services/cache.py. Literal[...] so a typo aborts at
    # import rather than silently selecting a backend.
    #
    # The default is "memory" rather than search-agent's "redis": this service
    # ships without a Redis, so opting in has to be deliberate.
    cache_backend: Literal["redis", "memory", "disabled"] = "memory"
    cache_redis_url: str = "redis://redis:6379/0"

    # Hosts allowed to reach the MCP endpoint (DNS-rebind / Host-header
    # protection). Passing any value turns the SDK's rebind protection ON, so a
    # request whose Host header isn't listed gets a silent 421. The ``:*`` suffix
    # is a wildcard-port match, since the forwarded Host varies by proxy/client;
    # add the public domain here if the reverse proxy forwards it verbatim.
    mcp_allowed_hosts: list[str] = ["eu-funding-tenders-portal-agent:*", "localhost:*"]

    debug: bool = False


# Built once at import time; a missing required var aborts startup here.
settings = Settings()
