"""Transport-level tests.

The multipart-encoding test is the important one: sending ``query``/``languages``
as ordinary form fields returns 500 from the real API, and a maintainer
"simplifying" ``_blob`` would reintroduce that. The mock can't reproduce the
failure, so we assert on the bytes we send instead.
"""

import httpx
import pytest
import respx

from app.services import portal_api
from tests.conftest import PORTAL_URL


def _parts(request: httpx.Request) -> str:
    return request.content.decode("utf-8", errors="replace")


@respx.mock
async def test_query_and_languages_are_sent_as_json_file_parts(portal_page):
    """Both parts need a filename AND Content-Type: application/json.

    As a plain form field the API answers 500 {"type":"throwable"}; in the URL,
    `query` is accepted and then silently ignored.
    """
    route = respx.post(PORTAL_URL).mock(return_value=httpx.Response(200, json=portal_page))
    await portal_api.search({"bool": {"must": []}}, languages=["en"])

    body = _parts(route.calls.last.request)
    assert 'name="query"; filename="blob"' in body
    assert 'name="languages"; filename="blob"' in body
    # Two parts, each declaring the JSON content type.
    assert body.count("Content-Type: application/json") == 2
    assert '["en"]' in body


@respx.mock
async def test_search_sends_the_wildcard_text_and_api_key(portal_page):
    route = respx.post(PORTAL_URL).mock(return_value=httpx.Response(200, json=portal_page))
    await portal_api.search({"bool": {"must": []}}, languages=["en"], page_size=25, page_number=3)

    params = route.calls.last.request.url.params
    assert params["apiKey"] == "SEDIA"
    assert params["text"] == "***"  # the portal's "no free-text term" wildcard
    assert params["pageSize"] == "25"
    assert params["pageNumber"] == "3"


@respx.mock
async def test_search_raises_on_http_error():
    respx.post(PORTAL_URL).mock(
        return_value=httpx.Response(
            400, json={"type": "businessError", "message": "Invalid Query"}
        )
    )
    with pytest.raises(httpx.HTTPStatusError):
        await portal_api.search({"wildcard": {"identifier": "*MISS*"}}, languages=["en"])


# --- pagination --------------------------------------------------------------


def _page(count: int, total: int = 1000) -> dict:
    return {
        "totalResults": total,
        "results": [
            {"metadata": {"identifier": [f"T-{n}"], "status": ["31094501"]}} for n in range(count)
        ],
    }


@respx.mock
async def test_search_all_stops_on_a_short_page(monkeypatch):
    monkeypatch.setattr(portal_api.settings, "portal_page_size", 100)
    respx.post(PORTAL_URL).mock(
        side_effect=[
            httpx.Response(200, json=_page(100)),
            httpx.Response(200, json=_page(30)),  # short → last page
        ]
    )
    rows = await portal_api.search_all({}, languages=["en"])
    assert len(rows) == 130


@respx.mock
async def test_search_all_stops_once_total_results_is_reached(monkeypatch):
    monkeypatch.setattr(portal_api.settings, "portal_page_size", 10)
    respx.post(PORTAL_URL).mock(
        side_effect=[
            httpx.Response(200, json=_page(10, total=20)),
            httpx.Response(200, json=_page(10, total=20)),
        ]
    )
    rows = await portal_api.search_all({}, languages=["en"])
    assert len(rows) == 20


@respx.mock
async def test_search_all_logs_when_the_page_cap_truncates(monkeypatch, caplog):
    """Silent truncation would make the ranking quietly incomplete."""
    monkeypatch.setattr(portal_api.settings, "portal_page_size", 10)
    respx.post(PORTAL_URL).mock(return_value=httpx.Response(200, json=_page(10)))
    rows = await portal_api.search_all({}, languages=["en"], max_pages=2)
    assert len(rows) == 20
    assert "may be incomplete" in caplog.text


# --- one topic by identifier -------------------------------------------------


def _row(identifier: str, *, status="31094501", description="Real description"):
    metadata = {"identifier": [identifier], "descriptionByte": [description]}
    if status:
        metadata["status"] = [status]
    return {"metadata": metadata, "language": "en"}


def test_best_row_skips_index_stubs():
    """For HORIZON-CL5-2024-D6-01-05 the row that sorted FIRST had no status,
    no deadline and an empty description — results[0] would return that one."""
    stub = _row("X", status=None, description="")
    real = _row("X")
    assert portal_api._best_row([stub, real]) is real


def test_best_row_falls_back_when_every_row_is_a_stub():
    stub = _row("X", status=None, description="")
    assert portal_api._best_row([stub]) is stub


def test_best_row_of_nothing_is_none():
    assert portal_api._best_row([]) is None


@respx.mock
async def test_get_by_identifier_returns_the_richest_row():
    respx.post(PORTAL_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    _row("HORIZON-CL5-1", status=None, description=""),
                    _row("HORIZON-CL5-1"),
                ]
            },
        )
    )
    row = await portal_api.get_by_identifier("HORIZON-CL5-1", languages=["en"])
    assert row["metadata"]["status"] == ["31094501"]


@respx.mock
async def test_get_by_identifier_returns_none_when_absent():
    respx.post(PORTAL_URL).mock(return_value=httpx.Response(200, json={"results": []}))
    assert await portal_api.get_by_identifier("NOPE", languages=["en"]) is None


# --- client lifecycle and readiness -----------------------------------------


async def test_client_is_reused_then_closed():
    first = portal_api._get_client()
    assert portal_api._get_client() is first  # lazy singleton
    await portal_api.close_client()
    assert portal_api._client is None
    await portal_api.close_client()  # idempotent


@respx.mock
async def test_ping_is_ok_when_the_portal_answers(portal_page):
    respx.post(PORTAL_URL).mock(return_value=httpx.Response(200, json=portal_page))
    assert await portal_api.ping() is True


@respx.mock
async def test_ping_is_false_when_the_portal_is_unreachable():
    respx.post(PORTAL_URL).mock(side_effect=httpx.ConnectError("no route"))
    assert await portal_api.ping() is False
