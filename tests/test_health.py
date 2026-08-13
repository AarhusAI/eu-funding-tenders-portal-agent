import httpx
import respx

from tests.conftest import PORTAL_URL


async def test_health_is_always_ok(client):
    """Liveness must not depend on the Portal — it answers if the process runs."""
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@respx.mock
async def test_ready_is_ok_when_the_portal_answers(client, portal_page):
    route = respx.post(PORTAL_URL).mock(return_value=httpx.Response(200, json=portal_page))
    response = await client.get("/health/ready")
    assert response.status_code == 200
    # Readiness must stay a cheap probe, not a corpus warm-up.
    assert route.calls.last.request.url.params["pageSize"] == "1"


@respx.mock
async def test_ready_is_503_when_the_portal_is_unreachable(client):
    respx.post(PORTAL_URL).mock(side_effect=httpx.ConnectError("no route"))
    response = await client.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "error"}
