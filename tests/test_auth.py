import pytest
from fastapi import HTTPException

from app.auth import is_valid_bearer, verify_api_key


def test_valid_token_accepted():
    assert is_valid_bearer("Bearer test-api-key") is True


def test_surrounding_whitespace_is_tolerated():
    assert is_valid_bearer("Bearer  test-api-key ") is True


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "test-api-key",  # no scheme
        "Basic test-api-key",  # wrong scheme
        "bearer test-api-key",  # scheme is case-sensitive
        "Bearer wrong-key",
        "Bearer ",
    ],
)
def test_invalid_headers_rejected(header):
    assert is_valid_bearer(header) is False


async def test_verify_api_key_passes_a_good_token():
    assert await verify_api_key("Bearer test-api-key") is None


async def test_verify_api_key_raises_401_with_challenge():
    with pytest.raises(HTTPException) as excinfo:
        await verify_api_key("Bearer nope")
    assert excinfo.value.status_code == 401
    assert excinfo.value.headers["WWW-Authenticate"] == "Bearer"


async def test_search_requires_auth(client):
    response = await client.post("/search", json={"query": "x"})
    assert response.status_code == 401
