import hmac

from fastapi import Header, HTTPException, status

from app.config import settings


def is_valid_bearer(authorization: str | None) -> bool:
    """Return True iff ``authorization`` is ``Bearer <api_key>`` with the right key.

    The pure comparison at the heart of auth, shared by ``verify_api_key`` (the
    FastAPI route dependency) and the ASGI middleware guarding the MCP mount —
    the latter can't use a route dependency because MCP is a mounted sub-app.
    """
    if authorization is None or not authorization.startswith("Bearer "):
        return False
    token = authorization.removeprefix("Bearer ").strip()
    # compare_digest, not ==: a plain string compare short-circuits on the first
    # differing byte, so its timing leaks how much of the token was guessed.
    return hmac.compare_digest(token, settings.api_key)


async def verify_api_key(authorization: str | None = Header(default=None)) -> None:
    """Reject the request unless it carries a valid ``Authorization: Bearer <token>``.

    Wired into routes as a FastAPI dependency (see ``routes/search.py``): it
    returns nothing and is used purely for its side effect of raising 401 when
    the token is missing or wrong. ``Header(default=None)`` tells FastAPI to
    pull the ``Authorization`` request header into this argument.
    """
    if not is_valid_bearer(authorization):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or invalid bearer token",
            # WWW-Authenticate is the HTTP-spec response header for a 401.
            headers={"WWW-Authenticate": "Bearer"},
        )
