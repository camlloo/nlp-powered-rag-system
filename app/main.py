"""FastAPI 服务入口。

工程基座：配置加载、JSON 日志、request_id 关联、统一错误响应、
健康检查；API 路由与 Vue 前端构建产物同源托管（frontend/dist）。
"""

from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from app.api import admin, auth, conversations, documents, health, jobs, kbs, qa, retrieval
from app.api.deps import SettingsDep
from app.core.config import get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging, set_request_id
from app.db.session import engine

FRONTEND_DIST = Path(__file__).resolve().parents[1] / "frontend" / "dist"


class RequestIdMiddleware(BaseHTTPMiddleware):
    """每个请求分配 request_id，响应头回传，日志与错误体统一携带。"""

    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = request.headers.get("x-request-id") or uuid4().hex
        set_request_id(request_id)
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    engine.dispose()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)

    if settings.cors_origin_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origin_list,
            allow_credentials=False,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    app.add_middleware(RequestIdMiddleware)
    register_exception_handlers(app)

    app.include_router(auth.router, prefix=settings.api_prefix)
    app.include_router(admin.router, prefix=settings.api_prefix)
    app.include_router(conversations.router, prefix=settings.api_prefix)
    app.include_router(documents.router, prefix=settings.api_prefix)
    app.include_router(health.router)
    app.include_router(kbs.router, prefix=settings.api_prefix)
    app.include_router(jobs.router, prefix=settings.api_prefix)
    app.include_router(retrieval.router, prefix=settings.api_prefix)
    app.include_router(qa.router, prefix=settings.api_prefix)

    if FRONTEND_DIST.is_dir():
        # 前端构建产物同源托管：API 路由优先，剩余路径进 SPA
        app.mount(
            "/assets",
            StaticFiles(directory=FRONTEND_DIST / "assets"),
            name="assets",
        )

        @app.get("/", include_in_schema=False)
        def index() -> FileResponse:
            return FileResponse(FRONTEND_DIST / "index.html")
    else:

        @app.get("/", include_in_schema=False)
        def root(settings: SettingsDep) -> dict:
            return {
                "app": settings.app_name,
                "docs": "/docs",
                "api_prefix": settings.api_prefix,
            }

    return app


app = create_app()
