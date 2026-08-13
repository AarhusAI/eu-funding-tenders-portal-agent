"""Fake SEDIA search API for local development.

Mimics the real EU Funding & Tenders Portal search endpoint closely enough to
drive the agent end-to-end offline — including its two hostile behaviours, which
a forgiving mock would hide:

* ``query``/``languages`` sent as ordinary form fields get a
  ``500 {"type": "throwable"}``, exactly as production does. This is the single
  most expensive failure mode in this integration, so the mock reproduces it
  rather than politely accepting whatever arrives.
* A query using ``wildcard`` or ``match`` gets a
  ``400 {"type": "businessError"}``: the real DSL accepts only
  ``bool``/``must``/``terms``.

Authentication is intentionally ignored — apiKey=SEDIA is public.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("mock-portal")

FIXTURES = Path(__file__).parent / "fixtures"
TOPICS: list[dict[str, Any]] = json.loads((FIXTURES / "topics.json").read_text())["results"]

app = FastAPI(title="mock-portal", description="Fake SEDIA search API for local dev")

# Query DSL clauses the real API accepts. Anything else is a businessError —
# notably `wildcard` and `match`, which is why identifier filtering has to run
# client-side in the agent.
SUPPORTED_CLAUSES = {"bool", "must", "terms"}

# Structural clauses whose values hold further clauses rather than field names.
COMPOUND_CLAUSES = {"bool", "must", "should", "must_not", "filter"}


def _first(metadata: dict[str, Any], key: str) -> Any:
    value = metadata.get(key)
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _clauses(node: Any) -> set[str]:
    """Every clause name appearing anywhere in the query tree.

    Recursion only continues through *compound* clauses. A leaf clause's keys are
    field names (``type``, ``status``, ``identifier``), not clause names — so
    descending into them would both reject every valid query and name fields as
    if they were the problem.
    """
    found: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            found.add(key)
            if key in COMPOUND_CLAUSES:
                found |= _clauses(value)
    elif isinstance(node, list):
        for item in node:
            found |= _clauses(item)
    return found


def _terms(query: Any) -> dict[str, list[str]]:
    """Flatten every {"terms": {field: [values]}} in the tree into one mapping."""
    out: dict[str, list[str]] = {}
    if isinstance(query, dict):
        for key, value in query.items():
            if key == "terms" and isinstance(value, dict):
                for field, values in value.items():
                    out.setdefault(field, []).extend(values)
            else:
                for field, values in _terms(value).items():
                    out.setdefault(field, []).extend(values)
    elif isinstance(query, list):
        for item in query:
            for field, values in _terms(item).items():
                out.setdefault(field, []).extend(values)
    return out


@app.post("/search-api/prod/rest/search")
async def search(request: Request) -> Any:
    form = await request.form()

    # --- reproduce the production failure modes ------------------------------
    for name in ("query", "languages"):
        value = form.get(name)
        if value is None:
            continue
        # A file part exposes .filename; a plain form field is just a string.
        if not hasattr(value, "filename"):
            log.warning("%s arrived as a plain form field — replying 500 like production", name)
            return JSONResponse(status_code=500, content={"type": "throwable"})

    raw_query = form.get("query")
    query = json.loads(await raw_query.read()) if raw_query is not None else {}
    raw_languages = form.get("languages")
    languages = json.loads(await raw_languages.read()) if raw_languages is not None else []

    unsupported = _clauses(query) - SUPPORTED_CLAUSES
    if unsupported:
        log.warning("unsupported query clauses %s — replying 400", sorted(unsupported))
        return JSONResponse(
            status_code=400,
            content={
                "apiVersion": "2.151",
                "type": "businessError",
                "message": f"Invalid Query format: {sorted(unsupported)}",
            },
        )

    # --- filter -------------------------------------------------------------
    wanted = _terms(query)
    rows = TOPICS
    for field, values in wanted.items():
        rows = [r for r in rows if str(_first(r["metadata"], field)) in {str(v) for v in values}]
    if languages:
        rows = [r for r in rows if r.get("language") in languages]

    page_size = int(request.query_params.get("pageSize", 100))
    page_number = int(request.query_params.get("pageNumber", 1))
    start = (page_number - 1) * page_size
    page = rows[start : start + page_size]
    log.info(
        "search: %d topics -> %d after filters, page %d returns %d",
        len(TOPICS),
        len(rows),
        page_number,
        len(page),
    )
    return {
        "apiVersion": "2.151",
        "terms": request.query_params.get("text", "***"),
        "totalResults": len(rows),
        "pageNumber": page_number,
        "pageSize": page_size,
        "sort": "relevance",
        "results": page,
        "warnings": [],
    }


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
