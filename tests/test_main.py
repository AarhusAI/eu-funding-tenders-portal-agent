"""The MCP mount's auth middleware.

It is pure ASGI rather than BaseHTTPMiddleware on purpose: the MCP transport
replies with long-lived SSE streams, which BaseHTTPMiddleware buffers and breaks.
"""


async def test_mcp_requires_a_bearer_token(client):
    response = await client.post("/mcp", json={})
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


async def test_mcp_rejects_a_wrong_token(client):
    response = await client.post("/mcp", json={}, headers={"Authorization": "Bearer nope"})
    assert response.status_code == 401


async def test_mcp_passes_a_valid_token_through(api_headers):
    """Past the middleware the MCP transport takes over; any status other than
    401 proves the request was let through.

    This runs inside the app lifespan on purpose. Without it the transport
    raises "Task group is not initialized" — the exact failure the lifespan
    wiring in main.py exists to prevent, since Starlette never runs a mounted
    sub-app's own lifespan.
    """
    from httpx import ASGITransport, AsyncClient

    from app.main import app as fastapi_app
    from app.main import lifespan

    async with lifespan(fastapi_app):
        transport = ASGITransport(app=fastapi_app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            response = await ac.post("/mcp", json={}, headers=api_headers)
    assert response.status_code != 401


async def test_non_mcp_paths_are_untouched_by_the_middleware(client):
    assert (await client.get("/health")).status_code == 200
