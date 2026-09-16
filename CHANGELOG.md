# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Pluggable corpus cache** (`services/cache.py`), selected with `CACHE_BACKEND`: `memory`
  (default, unchanged behaviour), `redis` (shared across instances and across restarts, via
  `CACHE_REDIS_URL`) or `disabled`. Redis operations fail open — an unreachable or slow cache is
  treated as a miss and logged, so an outage makes searches slow rather than failing them. Cache
  keys are namespaced and versioned (`corpus:v1:<sha256>`); bump `_NAMESPACE_VERSION` when the shape
  of a cached topic changes instead of flushing the store. Ported from the sibling
  [`AarhusAI/search-agent`](https://github.com/AarhusAI/search-agent) so both services cache alike.
- Optional `redis` service in `docker-compose.yml` behind a `redis` compose profile, plus
  `task up:redis`. A plain `task up` still starts only the agent.
- Settings for values that were previously hardcoded: `AGENT_SYSTEM_PROMPT`, `AGENT_MAX_RESULTS`,
  `AGENT_MAX_RESULTS_CAP`, `AGENT_PREVIEW_CHARS`, `MCP_MAX_DESCRIPTION_CHARS`, `MCP_MAX_KEYWORDS`.
  All default to the previous constants, so behaviour is unchanged unless set.
- `PORTAL_PROGRAMME_IDS` and `PORTAL_STATUS_IDS` — JSON objects merged over the verified built-in
  Portal taxonomy ids, so a newly published programme is an env var rather than a release.

### Fixed

- `keywords` is now returned as a real `list[str]` on every row. The Portal serves the field in two
  shapes — usually a plain list, but sometimes a single-element list holding a JSON array encoded as
  text — and the encoded form was passed through verbatim, so callers got one ~1 KB escaped string
  instead of keywords. It also defeated `MCP_MAX_KEYWORDS`: capping a 1-element list is a no-op, so
  the full blob was replayed into the model's context on every chat turn. Ranking was unaffected and
  is unchanged (verified), since substring matching already found the terms inside the blob.
- `AGENT_MAX_ITERATIONS` was declared but never read, so setting it did nothing. It is now enforced
  as a tool-call budget via PydanticAI's `UsageLimits`. Exceeding it is not an error: the run ends
  and the caller keeps the topics the tools already gathered.

### Removed

- `HOST` and `PORT` settings. Neither was ever read — the Dockerfile's `uvicorn` command passes
  `--host`/`--port` directly — so they could not take effect and only implied configurability that
  did not exist.

### Changed

- `services/corpus.py` no longer owns the cache: the module-level dicts and TTL check moved into
  `InMemoryBackend`. `corpus.invalidate()` is removed — tests isolate with
  `cache.set_backend_for_testing()` instead.
- The cache backend is initialised in the app lifespan (`init_cache` / `close_cache`), so the chosen
  backend appears in the boot log and a bad configuration fails at startup rather than first search.
- README rewritten in response to review: states plainly that the Portal API is unofficial and
  reverse-engineered, explains what a search profile is and what `clusters` / `topic_contains` mean,
  gives example questions, and clarifies that `PORTAL_LANGUAGES` selects which language *copy* is
  indexed and is not how to get translated answers.

### Added (initial implementation)

- Initial implementation of the EU Funding & Tenders Portal agent.
- `POST /search` — natural-language search over Portal call topics, accepting a search profile that
  filters by programme, status, topic identifier and cluster, and ranks by keyword.
- `POST /mcp` — the same search as an MCP tool over Streamable HTTP, with a compacted payload so a
  chat client replaying tool results doesn't overflow the model's context.
- `services/portal_api.py` — SEDIA search client. `query` and `languages` are sent as
  multipart file parts, which is the only encoding the API accepts.
- `services/corpus.py` — trims, dedupes and caches the profile-scoped topic corpus in-process with
  a TTL, keyed on `(query, languages)`.
- `services/profile.py` — pure filtering and additive keyword ranking; metadata hits score 2,
  description-only hits score 1.
- Configurable retrieval language via `PORTAL_LANGUAGES`, overridable per request, with per-topic
  fallback when several are given.
- `/health` and `/health/ready`, the latter probing the Portal with a `pageSize=1` search.
- `dev/mock-portal` and `dev/mock-llm` for a fully offline `task try:local`; the mock Portal
  reproduces the real API's 500 on plain-form parameters and 400 on unsupported query clauses.
- 100% test coverage, enforced with `--cov-fail-under=100`.

### Notes

Dependency pins differ deliberately from the sibling agents, whose current pins no longer resolve to
an importable install:

- `mcp[cli]>=2.0,<3` — SDK 2.0 renamed `FastMCP` to `MCPServer` and moved `transport_security` from
  the constructor to `streamable_http_app()`. The `mcp.server.fastmcp` import path is gone.
- `pydantic-ai-slim[openai]>=2.0,<3` — the siblings' `<1.0` pin resolves to 0.8.1, which imports
  `opentelemetry._events`, a module removed from current `opentelemetry-api`. `OpenAIModel` is now
  `OpenAIChatModel`.
