"""Admin-only original story queue. No model invocation in the HTTP process."""
from fastapi import APIRouter, Depends, HTTPException, Request, Response

from src.api.routes.script_review_routes import read_admin_body
from src.core.auth_middleware import get_current_admin_user_from_request
from src.fusion.creative_generation import CreativeError, CreativeRequest, CreativeStore

router = APIRouter(prefix="/api/admin/fusion/creative-jobs", tags=["AI原创剧本"])


def creative_store(request: Request) -> CreativeStore:
    get_current_admin_user_from_request(request)
    return CreativeStore()


@router.get("")
async def list_jobs(request: Request, response: Response, store: CreativeStore = Depends(creative_store)) -> dict:
    get_current_admin_user_from_request(request)
    response.headers["Cache-Control"] = "no-store"
    return {"success": True, "data": store.list()}


@router.post("", status_code=202)
async def create_job(request: Request, response: Response, store: CreativeStore = Depends(creative_store)) -> dict:
    actor = get_current_admin_user_from_request(request)
    body = await read_admin_body(request, CreativeRequest, 16384)
    response.headers["Cache-Control"] = "no-store"
    try:
        return {"success": True, "data": store.create(body, getattr(actor, "id"))}
    except CreativeError as exc:
        raise HTTPException(409, str(exc)) from None


@router.post("/{identifier}/cancel")
async def cancel_job(identifier: str, request: Request, response: Response, store: CreativeStore = Depends(creative_store)) -> dict:
    get_current_admin_user_from_request(request)
    response.headers["Cache-Control"] = "no-store"
    try:
        return {"success": True, "data": store.cancel(identifier)}
    except CreativeError as exc:
        raise HTTPException(409, str(exc)) from None
