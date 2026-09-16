import json
import os
import pathlib

# Pydantic-settings reads the environment at module import time, so these
# overrides MUST land before any `app` import. Anything below this block
# can safely import from app.* — see the agentic-tool guide.
os.environ["API_KEY"] = "test-api-key"
os.environ["AGENT_API_KEY"] = "test-agent-key"
os.environ["AGENT_API_BASE_URL"] = "http://fake-llm:4000/v1"
os.environ["PORTAL_SEARCH_URL"] = "http://fake-portal/search-api/prod/rest/search"
os.environ["PORTAL_LANGUAGES"] = '["en"]'
os.environ["DEBUG"] = "true"

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.services import cache, corpus, portal_api

FIXTURES = pathlib.Path(__file__).parent / "fixtures"

# The real Portal endpoint is a single URL, not a base + path, so tests match
# on this exact string.
PORTAL_URL = "http://fake-portal/search-api/prod/rest/search"


@pytest.fixture
def api_headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-api-key"}


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture(autouse=True)
async def _reset_state():
    """Module-level clients and caches leak between tests unless reset.

    A fresh ``InMemoryBackend`` per test is the isolation: the app never calls
    ``init_cache()`` here (no lifespan runs for the shared ``client`` fixture),
    so without this every test would get the disabled backend and the corpus
    tests that count Portal round-trips would stop meaning anything.
    """
    cache.set_backend_for_testing(cache.InMemoryBackend())
    yield
    await portal_api.close_client()
    cache.set_backend_for_testing(None)


@pytest.fixture
def portal_page() -> dict:
    """A real (trimmed) Portal search response — see .tmp/make_fixtures.py."""
    return json.loads((FIXTURES / "portal_search_page.json").read_text())


@pytest.fixture
def identifiers() -> dict[str, str]:
    """Name -> real topic identifier, so tests don't hardcode topic codes."""
    return json.loads((FIXTURES / "identifiers.json").read_text())


@pytest.fixture
def topics(portal_page) -> list[dict]:
    """The fixture page already trimmed into the corpus's internal shape."""
    return [corpus.trim(row) for row in portal_page["results"]]


@pytest.fixture
def empty_page() -> dict:
    return {"totalResults": 0, "pageNumber": 1, "pageSize": 100, "results": []}
