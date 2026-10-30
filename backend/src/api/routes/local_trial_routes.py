"""Active administrators may explicitly approve a labelled local trial on loopback."""
from fastapi import APIRouter, Depends, HTTPException, Request, Response, Query
from sqlalchemy.orm import Session

from src.api.routes.script_publication_routes import _invoke
from src.api.routes.script_review_routes import _body, _identifier
from src.api.routes.source_bundle_routes import source_store
from src.core.auth_middleware import get_current_admin_user_from_request
from src.db.session import get_db_session
from src.fusion.local_trial_publication import LocalTrialPublicationService, local_trials_enabled, loopback_request
from src.fusion.source_bundles import SourceBundleStore
from src.schemas.script_publication import ApprovePublicationRequest, PublishPackageRequest

router = APIRouter(prefix="/api/admin/fusion", tags=["本地试玩发布"])


def trial_service(request: Request, db: Session = Depends(get_db_session)):
    get_current_admin_user_from_request(request)
    if not local_trials_enabled() or not loopback_request(request):
        raise HTTPException(403, "本地试玩仅在显式启用的开发环境回环地址可用")
    return LocalTrialPublicationService(db)


@router.get("/script-packages/{version_id}/local-trial")
async def state(version_id: str, request: Request, response: Response,
                bundle_hash: str = Query(pattern=r"^[0-9a-f]{64}$"),
                service=Depends(trial_service), store: SourceBundleStore = Depends(source_store)):
    response.headers["Cache-Control"] = "no-store"
    return _invoke(lambda: service.gate_state(_identifier(version_id), store, bundle_hash))


@router.post("/script-packages/{version_id}/local-trial/approvals")
async def approve(version_id: str, request: Request, response: Response,
                  service=Depends(trial_service), store: SourceBundleStore = Depends(source_store)):
    actor = get_current_admin_user_from_request(request)
    body = await _body(request, ApprovePublicationRequest, 512 * 1024)
    response.headers["Cache-Control"] = "no-store"
    return _invoke(lambda: service.approve(_identifier(version_id), body, actor.id, store))


@router.post("/script-packages/{version_id}/local-trial/publish")
async def publish(version_id: str, request: Request, response: Response,
                  service=Depends(trial_service), store: SourceBundleStore = Depends(source_store)):
    actor = get_current_admin_user_from_request(request)
    body = await _body(request, PublishPackageRequest, 16384)
    response.headers["Cache-Control"] = "no-store"
    return _invoke(lambda: service.publish(_identifier(version_id), body, actor.id, store))
