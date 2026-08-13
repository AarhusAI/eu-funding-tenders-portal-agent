import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.auth import is_valid_bearer
from app.config import settings
from app.mcp_server import mcp
from app.routes.search import router as search_router
from app.services import portal_api

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown hook for the app.

    Code before ``yield`` runs once at startup; code after it runs at shutdown.
    We log the config on boot, run the MCP Streamable-HTTP session manager for
    the lifetime of the app, and close the shared HTTP client on exit.

    The ``session_manager.run()`` is required: FastAPI/Starlette does NOT invoke
    the lifespan of a sub-app attached via ``app.mount(...)``, so the MCP mount's
    own session manager would never start on its own — without this the ``/mcp``
    endpoint raises "Task group is not initialized".

    The topic corpus is deliberately *not* warmed here: startup stays fast and
    independent of the Portal being reachable, at the cost of the first search
    after a restart paying the fetch (see app/services/corpus.py).
    """
    log.info("starting eu-funding-tenders-portal-agent")
    log.info("agent_model=%s agent_api_base=%s", settings.agent_model, settings.agent_api_base_url)
    log.info(
        "portal_search_url=%s languages=%s cache_ttl=%ds",
        settings.portal_search_url,
        ",".join(settings.portal_languages),
        settings.portal_cache_ttl,
    )
    async with mcp.session_manager.run():
        yield  # app runs here, serving requests, until shutdown
    await portal_api.close_client()
    log.info("eu-funding-tenders-portal-agent stopped")


class MCPAuthMiddleware:
    """Require ``Authorization: Bearer <API_KEY>`` for requests to the MCP mount.

    Pure ASGI (not ``BaseHTTPMiddleware``) on purpose: the MCP transport replies
    with long-lived Server-Sent Event streams, and ``BaseHTTPMiddleware`` buffers
    and breaks streaming / client-disconnect handling. This inspects the raw scope
    and either short-circuits with a 401 or passes the request through untouched,
    leaving the SSE stream intact. Only ``/mcp`` paths are guarded; the REST routes
    keep their own ``Depends(verify_api_key)``.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("path", "").startswith("/mcp"):
            headers = dict(scope.get("headers") or [])
            raw = headers.get(b"authorization")
            authorization = raw.decode("latin-1") if raw is not None else None
            if not is_valid_bearer(authorization):
                response = PlainTextResponse(
                    "missing or invalid bearer token",
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
                await response(scope, receive, send)
                return
        await self._app(scope, receive, send)


app = FastAPI(
    title="eu-funding-tenders-portal-agent",
    description="Natural-language search agent over the EU Funding & Tenders Portal",
    version="0.1.0",
    lifespan=lifespan,
)
app.add_middleware(MCPAuthMiddleware)

# Mount the /search route defined in app/routes/search.py.
app.include_router(search_router)


@app.get("/health")
async def health() -> dict:
    """Liveness probe — always returns ok if the process is running."""
    return {"status": "ok"}


@app.get("/health/ready")
async def health_ready():
    """Readiness probe — ok only if the Portal search API is reachable.

    Used by orchestrators (e.g. Kubernetes) to decide whether to send traffic;
    returns 503 when the upstream can't be reached so we aren't marked ready.
    """
    if await portal_api.ping():
        return {"status": "ok"}
    return JSONResponse(status_code=503, content={"status": "error"})


# Mount the MCP Streamable-HTTP server LAST so the explicit routes above take
# precedence. The default ``streamable_http_path="/mcp"`` is an exact Route with
# no trailing-slash redirect, so mounting the sub-app at "/" serves the live
# endpoint at ``POST /mcp``. Auth is enforced by MCPAuthMiddleware above.
#
# transport_security belongs here rather than on the MCPServer constructor: SDK
# 2.0 moved it. Supplying allowed_hosts turns DNS-rebind protection ON, so a
# request whose Host header isn't listed is rejected with 421.
app.mount(
    "/",
    mcp.streamable_http_app(
        transport_security=TransportSecuritySettings(allowed_hosts=settings.mcp_allowed_hosts),
    ),
)
