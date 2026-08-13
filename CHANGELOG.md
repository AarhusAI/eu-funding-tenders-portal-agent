# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

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
