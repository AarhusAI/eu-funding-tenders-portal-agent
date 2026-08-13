from fastapi import APIRouter, Depends

from app.auth import verify_api_key
from app.models import SearchRequest, SearchResponse
from app.services.results import run_search

router = APIRouter()


@router.post("/search", response_model=SearchResponse)
async def search(request: SearchRequest, _: None = Depends(verify_api_key)) -> SearchResponse:
    """The HTTP shell for /search: authenticate, run the agent, shape the reply.

    ``Depends(verify_api_key)`` runs the auth check before this body; the result
    is unused so the parameter is named ``_`` (Python convention for "ignore").
    The agent run and result shaping live in ``services/results.run_search`` so
    the MCP tool can reuse exactly the same path.
    """
    return await run_search(
        query=request.query,
        max_results=request.max_results,
        language=request.language,
        search_profile=request.profile,
    )
