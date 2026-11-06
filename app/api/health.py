"""健康检查：liveness 与 readiness。

readiness 只检查数据库与 pgvector 扩展，不调用付费模型（实现文档 §12.2）。
"""

from fastapi import APIRouter
from sqlalchemy import text

from app.api.deps import SessionDep
from app.core.errors import ServiceUnavailable
from app.schemas.health import LivenessResponse, ReadinessResponse

router = APIRouter(tags=["health"])


@router.get("/healthz", response_model=LivenessResponse)
def liveness() -> LivenessResponse:
    return LivenessResponse(status="ok")


@router.get("/readyz", response_model=ReadinessResponse)
def readiness(session: SessionDep) -> ReadinessResponse:
    try:
        pg_version = session.execute(text("SELECT version()")).scalar_one()
        ext_version = session.execute(
            text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        ).scalar_one_or_none()
    except Exception as exc:  # noqa: BLE001 就绪探针需把所有失败转为 503
        raise ServiceUnavailable(f"数据库不可用：{exc.__class__.__name__}") from exc
    if ext_version is None:
        raise ServiceUnavailable("pgvector 扩展未安装")
    return ReadinessResponse(
        status="ready",
        database="ok",
        pgvector=ext_version,
        pg_version=pg_version.split()[1],
    )
