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
    agent_max_iterations: int = 8  # max tool-calling rounds per query

    # --- EU Funding & Tenders Portal (SEDIA search API) ---
    # Public endpoint; "SEDIA" is the literal apiKey the portal frontend uses,
    # so no real credential is involved.
    portal_search_url: str = "https://api.tech.ec.europa.eu/search-api/prod/rest/search"
    portal_api_key: str = "SEDIA"
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

    # Hosts allowed to reach the MCP endpoint (DNS-rebind / Host-header
    # protection). Passing any value turns the SDK's rebind protection ON, so a
    # request whose Host header isn't listed gets a silent 421. The ``:*`` suffix
    # is a wildcard-port match, since the forwarded Host varies by proxy/client;
    # add the public domain here if the reverse proxy forwards it verbatim.
    mcp_allowed_hosts: list[str] = ["eu-funding-tenders-portal-agent:*", "localhost:*"]

    debug: bool = False
    host: str = "0.0.0.0"
    port: int = 8000


# Built once at import time; a missing required var aborts startup here.
settings = Settings()
