import os
import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Optional
from uuid import uuid4

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException, status
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError

from src.core.environment import load_project_environment

load_project_environment()

from src.core.security_preflight import is_production
from src.core.startup import initialize_application
from src.services.auth_service import AuthService
from src.db.session import db_manager

# 导入剧本管理相关路由
from src.api.routes.script_routes import router as script_management_router
from src.api.routes.script_editor_routes import router as script_editor_router
from src.api.routes.image_generation_routes import router as image_generation_router
from src.api.routes.evidence_routes import router as evidence_router
from src.api.routes.character_routes import router as character_router
from src.api.routes.location_routes import router as location_router
from src.api.routes.asset_routes import router as asset_router
# 导入文件管理API路由
from src.api.routes.file_routes import router as file_router
# 导入TTS API路由
from src.api.routes.tts_routes import router as tts_router
# 导入用户认证路由
from src.api.routes.auth_routes import router as auth_router
from src.api.routes.fusion_game_routes import router as fusion_router, admin_router as fusion_admin_router
from src.api.routes.script_package_routes import router as script_package_router
from src.api.routes.source_bundle_routes import router as source_bundle_router
from src.api.routes.script_review_routes import router as script_review_router
from src.api.routes.authoring_routes import router as authoring_router
from src.api.routes.creative_routes import router as creative_router
from src.api.routes.local_trial_routes import router as local_trial_router
from src.api.routes.script_publication_routes import router as script_publication_router
from src.api.routes.package_runtime_routes import router as package_runtime_router
from src.api.routes.package_flow_routes import router as package_flow_router
from src.api.routes.package_play_routes import router as package_play_router
from src.api.routes.package_speech_input_routes import router as package_speech_input_router
from src.fusion.package_speech_input import cleanup_speech_input_receipts
from src.fusion.service import FusionGameError, FusionGameService
from src.fusion.websocket import fusion_connections
from src.schemas.fusion_game import FusionActionRequest

from src.db.session import get_db_session


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理：启动时初始化，关闭时清理"""
    # --- startup ---
    try:
        initialize_application()
    except Exception:
        db_manager.close()
        logging.getLogger(__name__).exception("应用初始化失败")
        raise

    speech_cleanup = asyncio.create_task(cleanup_speech_input_receipts())
    try:
        yield
    finally:
        speech_cleanup.cancel()
        try:
            await speech_cleanup
        except asyncio.CancelledError:
            pass
        # --- shutdown ---
        try:
            db_manager.close()
            print("数据库连接池已关闭")
        except Exception as e:
            print(f"数据库关闭失败: {e}")


# 接口说明书（/docs、/redoc、/openapi.json）会完整列出全部接口、参数和数据结构。
# 此前它对公网无条件开放，等于把整套接口目录贴在门口。开发环境保留以便调试，
# 生产环境默认关闭；确需开放时设置 ENABLE_PUBLIC_API_DOCS=true 显式承担风险。
_expose_api_docs = (
    not is_production()
    or os.getenv("ENABLE_PUBLIC_API_DOCS", "false").lower() == "true"
)

app = FastAPI(
    title="雾幕档案",
    docs_url="/docs" if _expose_api_docs else None,
    redoc_url="/redoc" if _expose_api_docs else None,
    openapi_url="/openapi.json" if _expose_api_docs else None,
    lifespan=lifespan,
)


# httpx 默认在 INFO 级别打印每个请求的完整 URL。本服务刻意不把外部输入写进
# 日志正文，但这条库日志会把路径参数（可能是他人标识或私有编号）原样带进来，
# 出站调用时还可能包含查询串里的凭据。收紧到 WARNING，只保留异常信息。
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


@app.middleware("http")
async def request_trace(request: Request, call_next):
    """使用不含请求正文的结构化访问日志，避免泄露 JWT 和私本。"""
    request_id = request.headers.get("X-Request-ID", uuid4().hex)
    started = time.perf_counter()
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    logging.getLogger("access").info(
        "request_complete",
        extra={"request_id": request_id, "method": request.method, "path": request.url.path,
               "status_code": response.status_code, "duration_ms": round((time.perf_counter() - started) * 1000, 2)},
    )
    return response

# 添加全局验证错误处理器
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """处理请求验证错误，提供详细的错误信息"""
    logger = logging.getLogger(__name__)
    
    # 记录详细的错误信息
    # 只回报"哪个字段、错在哪类"，绝不回显收到的原始值。
    # 校验失败的请求可能携带私本正文、令牌或他人标识；把它原样写进响应体和
    # 日志，会让一个格式错误变成信息泄露，也与本服务不记录请求正文的约定相悖。
    error_details = []
    for error in exc.errors():
        error_details.append({
            "field": ".".join(str(x) for x in error["loc"]),
            "message": error["msg"],
            "type": error["type"],
        })

    # 同理只记录路由模板与字段名，不记录完整 URL（路径参数和查询串同样是外部输入）。
    route = request.scope.get("route")
    route_template = getattr(route, "path", None) or "unmatched"
    logger.error(
        "请求验证失败 - 方法: %s, 路由: %s, 字段: %s",
        request.method,
        route_template,
        [item["field"] for item in error_details],
    )

    return JSONResponse(
        status_code=422,
        content={
            "success": False,
            "message": "请求数据验证失败",
            "errors": error_details,
            "detail": "请检查请求数据格式和字段类型"
        }
    )

# 中间件注册：Starlette 后注册先执行，所以 CORS 必须最后注册（最外层先运行）
# 顺序：请求进来 → CORS → Auth → 路由处理
from src.core.auth_middleware import UnifiedAuthMiddleware

# 1. 先注册 Auth（内层，在 CORS 之后执行）
app.add_middleware(UnifiedAuthMiddleware)

# 2. 后注册 CORS（外层，最先执行，保证预检请求能通过）
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in os.getenv(
        "CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000"
    ).split(",") if origin.strip()],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
)

# 数据模型已迁移到各自的路由文件中


# 挂载静态文件目录（仅当目录存在时才挂载，避免启动崩溃）
if os.path.isdir("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")

# 遗留的全 AI 模拟器与全知剧本管理接口。
#
# 它们属于早期的"全部角色都由 AI 演"的旧玩法，以及返回全知视角（含凶手身份、
# 他人私本）的剧本编辑接口，当前单真人玩法一律走 /api/fusion。此前它们只是被
# 中间件的 ADMIN 规则挡着，但十几个入口依然照常注册——每一个都是需要持续维护、
# 也可能出问题的对外入口。
#
# 现在改为按开关决定是否注册：开发环境默认开启（旧管理页面仍可使用），
# 生产环境默认关闭（这些工具不该对外部署）。需要时可显式覆盖。
ENABLE_LEGACY_ADMIN = os.getenv(
    "ENABLE_LEGACY_ADMIN",
    "false" if is_production() else "true",
).lower() == "true"

# 注册剧本管理相关路由
if ENABLE_LEGACY_ADMIN:
    app.include_router(script_management_router)
    app.include_router(script_editor_router)
    app.include_router(evidence_router)
    app.include_router(character_router)
    app.include_router(location_router)
    # 遗留的全 AI 模拟器与全知对局历史已删除（前端入口已同步移除）。
    print("遗留管理接口已注册（ENABLE_LEGACY_ADMIN=true）")
else:
    print("遗留管理接口未注册（ENABLE_LEGACY_ADMIN=false）")

if os.getenv("ENABLE_MEDIA_FEATURES", "false").lower() == "true":
    app.include_router(image_generation_router)
# 注册文件管理API路由
app.include_router(file_router)
# 注册TTS API路由
if os.getenv("ENABLE_MEDIA_FEATURES", "false").lower() == "true":
    app.include_router(tts_router)
app.include_router(asset_router)
# 注册用户认证路由
app.include_router(auth_router)
app.include_router(fusion_router)
app.include_router(fusion_admin_router)
app.include_router(script_package_router)
app.include_router(source_bundle_router)
app.include_router(script_review_router)
app.include_router(authoring_router)
app.include_router(creative_router)
app.include_router(local_trial_router)
app.include_router(script_publication_router)
app.include_router(package_runtime_router)
app.include_router(package_flow_router)
app.include_router(package_play_router)
app.include_router(package_speech_input_router)


@app.get("/health")
def health():
    """容器和负载均衡器使用的轻量健康检查。"""
    return {"status": "ok", "service": "ai-jubensha-fusion"}


@app.websocket("/api/fusion/ws/{session_id}")
async def fusion_websocket(websocket: WebSocket, session_id: str, token: str):
    """认证后订阅状态，并复用 REST 的权威行动服务。"""
    db = db_manager.get_session()
    user = None
    try:
        user = AuthService.get_user_from_token(db, token)
        if not user or not user.is_active:
            await websocket.close(code=1008, reason="Authentication required")
            return
        games = FusionGameService(db)
        try:
            state = games.get_state(session_id, user.id)
        except FusionGameError:
            await websocket.close(code=1008, reason="Session not available")
            return
        await fusion_connections.connect(session_id, websocket)
        await websocket.send_json({"type": "FULL_STATE", "session_id": session_id, "payload": state})
        while True:
            try:
                body = FusionActionRequest.model_validate(await websocket.receive_json())
                before_event_id = games.get_state(session_id, user.id)["last_event_id"]
                state = games.perform_action(session_id, user.id, body.type, body.payload, body.idempotency_key)
                if body.type == "ask_question":
                    await websocket.send_json({"type": "AI_THINKING", "session_id": session_id, "payload": {"character_id": body.payload.get("target_character_id")}})
                    await games.answer_question(session_id, user.id, body.idempotency_key)
                    state = games.get_state(session_id, user.id)
                elif body.type == "advance_phase" and state["phase"] in ("INTRODUCTION", "DISCUSSION"):
                    await websocket.send_json({"type": "AI_THINKING", "session_id": session_id, "payload": {"phase": state["phase"]}})
                    await games.run_ai_phase(session_id, user.id, body.idempotency_key)
                    state = games.get_state(session_id, user.id)
                events = games.get_events(session_id, user.id, before_event_id)
                await fusion_connections.broadcast(session_id, {
                    "type": "STATE_UPDATED",
                    "session_id": session_id,
                    "events": events,
                    "payload": state,
                })
            except (FusionGameError, ValueError) as exc:
                db.rollback()
                await websocket.send_json({"type": "ERROR", "session_id": session_id, "payload": {"message": str(exc)}})
    except WebSocketDisconnect:
        pass
    finally:
        fusion_connections.disconnect(session_id, websocket)
        db.close()

# create_response函数已迁移到各自的路由文件中

if __name__ == "__main__":
    import uvicorn

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", 8010))  # 与 main.py 保持一致

    print(f"Starting server on http://{host}:{port}")
    uvicorn.run(app, host=host, port=port)
