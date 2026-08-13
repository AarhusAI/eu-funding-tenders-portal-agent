# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Status: pre-bootstrap

Only `README.md` exists. The application has **not been scaffolded yet**. The README's Process
mandates building this agent on the AarhusAI agentic-tool pattern — see "References" in the README.
The sections below describe the **target** stack and conventions to follow when bootstrapping; the
`task` commands work only once the corresponding files (`Taskfile.yml`, `pyproject.toml`, etc.) exist.

## Purpose

An agentic tool integrating the EU Funding & Tenders Portal into AarhusAI. It retrieves data from
the Portal API, lets a user chat about it, and accepts a **search profile** in the prompt that
filters and ranks calls/topics. See `README.md` for the example profile.

### Search-profile semantics (domain rules)

A profile constrains and ranks Portal results:
- **Programmes**: e.g. "Horizon Europe", "Digital Europe".
- **Topic filter**: e.g. topics containing "MISS".
- **Clusters**: CL2, CL4, CL5 (call-topic codes like `HORIZON-CL5-2027-07-D3-16`).
- **Submission status**: only "Forthcoming" and "Open for submission".
- **Keyword ranking**: boost results matching the README keyword list (IoT, Dataspace,
  Digital Twin, GenAI, Smart City, etc.). Ranking is additive over the matched keywords.

## Target stack

- Python 3.11+ (3.12-slim container)
- FastAPI + `uvicorn[standard]` — standalone microservice on port 8000
- `httpx` (async) for Portal API + single-shot LLM calls
- `pydantic` v2 + `pydantic-settings` for schemas and env-driven config
- **PydanticAI** (`pydantic-ai-slim[openai]`) for the tool-calling agent loop
- **LiteLLM** proxy as the LLM gateway at `http://litellm:4000/v1` (OpenAI-compatible)
- `ruff` for lint + format; `pytest` + `pytest-asyncio` (`asyncio_mode=auto`) + `pytest-cov`;
  `respx` for mocking `httpx`
- Go Task (`Taskfile.yml`) as the command runner; multi-stage `Dockerfile` (base → dev → prod)

**LLM rule**: single completion (extract → respond) → plain `httpx`; more than one hit
(tool-calling, retries, multi-step) → PydanticAI.

## Target structure

```
app/
  main.py        # FastAPI app, lifespan, /health + /health/ready
  config.py      # pydantic-settings; required fields fail-fast at import
  auth.py        # Bearer-token middleware (hmac.compare_digest)
  models.py      # request/response schemas
  routes/        # thin handlers: auth dep, validate body, one service call
  services/      # business logic — one module per concern (portal client, agent loop, ranking)
tests/
  conftest.py    # set env vars BEFORE importing app; autouse reset of module-level clients
  ...
Taskfile.yml  Dockerfile  docker-compose.yml  pyproject.toml  .env.example
```

**Convention**: routes are thin, services hold logic.

## Architecture conventions

- **Config**: all runtime settings via env (`pydantic-settings`). Fields without a default are
  required and crash at import (fail-fast). Likely vars: `API_KEY`, `AGENT_MODEL`,
  `AGENT_API_BASE_URL` (litellm), `AGENT_API_KEY`, `AGENT_TIMEOUT`, `DEBUG`.
- **Clients**: module-level async `httpx` client, lazy-initialized and cached; closed in lifespan
  teardown. Eagerly init long-lived clients in lifespan to avoid first-request latency.
- **Auth**: `verify_api_key` Bearer dependency with constant-time compare; routes gate via
  `Depends(verify_api_key)`.
- **Agent loop** (PydanticAI): per-request frozen-dataclass deps; `@agent.tool` for Portal queries;
  accumulate full results in `ctx.deps` while returning truncated previews to the LLM; wrap
  `agent.run(...)` in `asyncio.wait_for(timeout=...)`.
- **Health**: `/health` always 200 (liveness); `/health/ready` probes downstream, 503 on failure.

## Commands (available after bootstrap)

```bash
task up / task down       # start/stop dev container (--reload)
task shell                # shell into running container
task test                 # pytest in container
task test:coverage        # pytest --cov=app --cov-report=term-missing  (target: 100% per README)
task lint / task lint:fix # ruff check / ruff fix + format
task ci                   # lint + test (quality gate)
task try:local            # build mocks, start stack, run demo (mock LLM + mock API)
task try:realapi          # live Portal API with mock LLM
task build:image TAG=...  # multi-arch build & push to ghcr.io
```

Run a single test (inside container / via `task shell`): `pytest tests/path/test_x.py::test_name`.

## Testing conventions

- Set env vars in `conftest.py` **before** any `app` import — `Settings()` runs at import time.
- `client` fixture uses `ASGITransport` for in-process testing; `api_headers` fixture supplies Bearer auth.
- `autouse` fixture resets module-level clients between tests to prevent leakage.
- Mock at the call boundary: `respx` for the Portal API; mock the LiteLLM endpoint for the agent.
- README targets **100% test coverage**.
