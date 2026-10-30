"""CloudBase free deployment entry point; no legacy app, DB or .env startup.

    uvicorn src.cloudbase.app:create_app --factory --host 0.0.0.0 --port 8000 --no-proxy-headers

The gateway receives CLOUDBASE_ENV_ID and CLOUDBASE_API_KEY (or CloudRun's
secure CLOUDBASE_APIKEY binding); CORS_ORIGINS is an exact origin allowlist.
Player state and identity are durable in server-only PostgreSQL gateway RPCs.
Admin adapters can use the exported dependencies and ``app.include_router``;
this entry point does not invent approval records or expose a bootstrap route.
"""
from __future__ import annotations

import asyncio
import os
import re
from typing import Any, TypeVar
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from src.cloudbase.auth import CloudBaseAuth
from src.cloudbase.gateway import CloudBaseGateway, GatewayError, GatewayUnavailable
from src.cloudbase.play import CloudBasePlay, ManifestReader
from src.fusion.package_play import PackagePlayError
from src.fusion.package_runtime import PackageRuntimeError
from src.fusion.package_validation import parse_package_json
from src.schemas.package_play import CreatePackagePlayRequest, PackagePlayActionRequest, PackagePlaySpeakRequest
from src.schemas.package_runtime import CreatePackageSessionRequest


NO_STORE = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
MANIFEST_KEY = "rain-seventh-code-v2"
_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,95}\Z")
_SESSION = re.compile(r"package-[a-f0-9]{32}\Z")
_PLAY = re.compile(r"play-[a-f0-9]{32}\Z")
Model = TypeVar("Model", bound=BaseModel)


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=100)


class RefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    refresh_token: str = Field(min_length=1, max_length=128)


class EmptyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def _error(code: str, status: int) -> JSONResponse:
    safe = code if isinstance(code, str) and _CODE.fullmatch(code) else "CLOUDBASE_REQUEST_FAILED"
    status = status if type(status) is int and 400 <= status <= 599 else 500
    return JSONResponse({"detail": safe}, status_code=status, headers=NO_STORE)


def _cors_origins(raw: str) -> list[str]:
    if not isinstance(raw, str) or len(raw) > 4096:
        raise ValueError("CLOUDBASE_CORS_INVALID")
    result = []
    for value in raw.split(","):
        origin = value.strip()
        if not origin:
            continue
        try:
            parts = urlsplit(origin)
            host = parts.hostname
            _ = parts.port
        except ValueError:
            raise ValueError("CLOUDBASE_CORS_INVALID") from None
        if (len(result) >= 20 or not host or any(char in origin for char in "*\\%")
                or any(ord(char) < 33 for char in origin)
                or parts.username is not None or parts.password is not None
                or parts.path or parts.query or parts.fragment
                or (parts.scheme != "https" and not (
                    parts.scheme == "http" and host in {"localhost", "127.0.0.1", "::1"}))
                or origin != f"{parts.scheme}://{parts.netloc}"):
            raise ValueError("CLOUDBASE_CORS_INVALID")
        if origin not in result:
            result.append(origin)
    return result


async def read_body(request: Request, model: type[Model], *, limit: int = 8192,
                    allow_empty: bool = False) -> Model:
    """Bound bytes before JSON/Pydantic, reject duplicate fields and coercions."""
    raw = bytearray()
    lengths = request.headers.getlist("content-length")
    if lengths:
        if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdecimal() or len(lengths[0]) > 10:
            raise HTTPException(400, "CLOUDBASE_CONTENT_LENGTH_INVALID")
        if int(lengths[0]) > limit:
            raise HTTPException(413, "CLOUDBASE_REQUEST_TOO_LARGE")
    async for chunk in request.stream():
        if len(raw) + len(chunk) > limit:
            raise HTTPException(413, "CLOUDBASE_REQUEST_TOO_LARGE")
        raw.extend(chunk)
    if raw and request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(415, "CLOUDBASE_JSON_REQUIRED")
    try:
        value = {} if allow_empty and not raw else parse_package_json(bytes(raw))
        parsed = model.model_validate(value)
        parsed.model_dump_json().encode("utf-8")
        return parsed
    except (ValidationError, ValueError, UnicodeError):
        raise HTTPException(422, "CLOUDBASE_REQUEST_INVALID") from None


def gateway_for(request: Request) -> CloudBaseGateway:
    return request.app.state.cloudbase_gateway


def auth_for(request: Request) -> CloudBaseAuth:
    return request.app.state.cloudbase_auth


def _client_id(request: Request) -> str:
    # Proxy headers are deliberately ignored. Configure uvicorn with
    # --no-proxy-headers so ASGI peer identity cannot be supplied by a browser.
    if request.client is None or not request.client.host:
        raise HTTPException(400, "AUTH_CLIENT_INVALID")
    return request.client.host


def _bearer(request: Request) -> str:
    values = request.headers.getlist("authorization")
    if len(values) != 1 or len(values[0]) > 160:
        raise HTTPException(401, "AUTH_SESSION_INVALID")
    parts = values[0].split(" ")
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
        raise HTTPException(401, "AUTH_SESSION_INVALID")
    return parts[1]


async def active_user(request: Request) -> dict[str, Any]:
    cached = getattr(request.state, "cloudbase_user", None)
    if cached is not None:
        return cached
    user = await auth_for(request).me(_bearer(request))
    if (type(user.get("id")) is not int or user["id"] <= 0
            or user.get("is_active") is not True):
        raise HTTPException(401, "AUTH_SESSION_INVALID")
    request.state.cloudbase_user = user
    return user


async def _manifest(gateway: CloudBaseGateway) -> dict:
    record = await gateway.get("preview_manifest", MANIFEST_KEY, "system")
    if record is None:
        raise GatewayUnavailable("CLOUDBASE_MANIFEST_NOT_INSTALLED")
    try:
        manifest = record["payload"]
        if type(manifest) is not dict:
            raise ValueError("CLOUDBASE_MANIFEST_INVALID")
        ManifestReader([manifest])  # includes the exact original hash/basis check
        return manifest
    except (ValueError, KeyError, TypeError):
        raise GatewayUnavailable("CLOUDBASE_MANIFEST_INVALID") from None


async def play_for(request: Request) -> CloudBasePlay:
    state = request.app.state
    if state.cloudbase_play is None:
        async with state.cloudbase_play_lock:
            if state.cloudbase_play is None:
                manifest = await _manifest(gateway_for(request))
                state.cloudbase_play = CloudBasePlay(gateway_for(request), [manifest])
    return state.cloudbase_play


def _identifier(value: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise HTTPException(422, "CLOUDBASE_IDENTIFIER_INVALID")
    return value


async def _live_play(request: Request) -> CloudBasePlay:
    play = await play_for(request)
    if isinstance(play, CloudBasePlay):
        # Resolve the backend adapter for every request; the play service reads
        # current publication authority on every operation, never a cached flag.
        from src.cloudbase.admin_routes import admin_for
        play.authority = admin_for(request)
    return play


async def _run(request: Request, user: dict[str, Any], operation: str, body: dict) -> dict:
    play = await _live_play(request)
    return {"success": True, "data": await play.run(user["id"], operation, body)}


def create_app(*, gateway: CloudBaseGateway | None = None, auth: CloudBaseAuth | None = None,
               manifests: list[dict] | None = None) -> FastAPI:
    if gateway is None:
        env_id = os.environ.get("CLOUDBASE_ENV_ID", "")
        api_key = os.environ.get("CLOUDBASE_API_KEY", "") or os.environ.get("CLOUDBASE_APIKEY", "")
        if not env_id or not api_key:
            raise RuntimeError("CLOUDBASE_CONFIG_REQUIRED")
        gateway = CloudBaseGateway(env_id, api_key)
    origins = _cors_origins(os.environ.get("CORS_ORIGINS", ""))
    app = FastAPI(title="雾幕档案", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.cloudbase_gateway = gateway
    app.state.cloudbase_auth = auth if auth is not None else CloudBaseAuth(gateway)
    app.state.cloudbase_play_lock = asyncio.Lock()
    app.state.cloudbase_play = None if manifests is None else CloudBasePlay(gateway, manifests)

    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=False,
                       allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
                       allow_headers=["Authorization", "Content-Type", "Idempotency-Key"], max_age=600)

    @app.middleware("http")
    async def private_responses(request: Request, call_next):
        if len(request.scope.get("query_string", b"")) > 2048:
            return _error("CLOUDBASE_QUERY_TOO_LARGE", 413)
        try:
            response = await call_next(request)
        except Exception:
            # Stop unexpected library exceptions before ServerErrorMiddleware
            # re-raises/logs their potentially sensitive argument values.
            return _error("CLOUDBASE_REQUEST_FAILED", 500)
        response.headers.update(NO_STORE)
        return response

    @app.exception_handler(GatewayError)
    async def gateway_error(_request: Request, exc: GatewayError):
        return _error(exc.code, exc.status)

    @app.exception_handler(PackagePlayError)
    @app.exception_handler(PackageRuntimeError)
    async def package_error(_request: Request, exc: PackagePlayError | PackageRuntimeError):
        return _error(exc.code, exc.status_code)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_request: Request, exc: StarletteHTTPException):
        code = exc.detail if isinstance(exc.detail, str) else "CLOUDBASE_REQUEST_FAILED"
        if exc.status_code == 404:
            code = "CLOUDBASE_ENDPOINT_NOT_AVAILABLE"
        elif exc.status_code == 405:
            code = "CLOUDBASE_METHOD_NOT_ALLOWED"
        return _error(code, exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, _exc: RequestValidationError):
        return _error("CLOUDBASE_REQUEST_INVALID", 422)

    @app.exception_handler(Exception)
    async def internal_error(_request: Request, _exc: Exception):
        # Never return exception strings, request bodies, private material or keys.
        return _error("CLOUDBASE_REQUEST_FAILED", 500)

    @app.get("/health")
    async def health():
        try:
            await _manifest(gateway)
        except (GatewayError, ValueError):
            return _error("CLOUDBASE_DEPENDENCY_UNAVAILABLE", 503)
        return {"status": "ok", "storage": "cloudbase-postgresql", "preview": "ready"}

    @app.post("/api/auth/anonymous-login")
    async def anonymous_login(request: Request):
        await read_body(request, EmptyRequest, limit=256, allow_empty=True)
        return await auth_for(request).guest(_client_id(request))

    @app.post("/api/auth/login")
    async def login(request: Request):
        body = await read_body(request, LoginRequest, limit=4096)
        return await auth_for(request).login(body.username, body.password, _client_id(request))

    @app.post("/api/auth/refresh")
    async def refresh(request: Request):
        body = await read_body(request, RefreshRequest, limit=1024)
        return await auth_for(request).refresh(body.refresh_token, _client_id(request))

    @app.get("/api/auth/me")
    async def me(user: dict = Depends(active_user)):
        return user

    @app.post("/api/auth/logout")
    async def logout(request: Request):
        await read_body(request, EmptyRequest, limit=256, allow_empty=True)
        return await auth_for(request).logout(_bearer(request))

    @app.get("/api/fusion/package-releases")
    async def releases(request: Request):
        play = await _live_play(request)
        return {"success": True, "data": await play.catalog()}

    @app.post("/api/fusion/package-sessions", status_code=201)
    async def opening_create(request: Request, user: dict = Depends(active_user)):
        body = await read_body(request, CreatePackageSessionRequest, limit=4096)
        return await _run(request, user, "opening.create", body.model_dump(mode="json"))

    @app.get("/api/fusion/package-sessions/{session_id}")
    async def opening_get(session_id: str, request: Request, user: dict = Depends(active_user)):
        return await _run(request, user, "opening.get", {"session_id": _identifier(session_id, _SESSION)})

    @app.post("/api/fusion/package-plays", status_code=201)
    async def play_create(request: Request, user: dict = Depends(active_user)):
        body = await read_body(request, CreatePackagePlayRequest)
        return await _run(request, user, "play.create", body.model_dump(mode="json"))

    @app.get("/api/fusion/package-plays")
    async def play_find(request: Request, opening_session_id: str = "", user: dict = Depends(active_user)):
        return await _run(request, user, "play.find", {
            "opening_session_id": _identifier(opening_session_id, _SESSION)})

    @app.get("/api/fusion/package-plays/{play_id}")
    async def play_get(play_id: str, request: Request, user: dict = Depends(active_user)):
        return await _run(request, user, "play.get", {"play_id": _identifier(play_id, _PLAY)})

    @app.post("/api/fusion/package-plays/{play_id}/actions")
    async def play_act(play_id: str, request: Request, user: dict = Depends(active_user)):
        body = await read_body(request, PackagePlayActionRequest)
        return await _run(request, user, "play.act", {
            "play_id": _identifier(play_id, _PLAY), "command": body.model_dump(mode="json")})

    @app.post("/api/fusion/package-plays/{play_id}/discussion")
    async def play_speak(play_id: str, request: Request, user: dict = Depends(active_user)):
        body = await read_body(request, PackagePlaySpeakRequest)
        return await _run(request, user, "play.speak", {
            "play_id": _identifier(play_id, _PLAY), "command": body.model_dump(mode="json")})

    @app.get("/api/fusion/package-play-library")
    async def library(request: Request, offset: str = "0", limit: str = "20", user: dict = Depends(active_user)):
        if any(len(value) > 10 or not value.isascii() or not value.isdecimal() for value in (offset, limit)):
            raise HTTPException(422, "PACKAGE_PLAY_LIBRARY_PAGE_INVALID")
        page_offset, page_limit = int(offset), int(limit)
        if not 1 <= page_limit <= 50:
            raise HTTPException(422, "PACKAGE_PLAY_LIBRARY_PAGE_INVALID")
        return await _run(request, user, "play.library", {"offset": page_offset, "limit": page_limit})

    from src.cloudbase.admin_routes import router as admin_router
    app.include_router(admin_router)
    return app
