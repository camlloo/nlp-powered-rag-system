"""统一业务错误与全局异常处理。

实现文档 §9.4：业务错误返回 code、message、request_id、retryable，
不暴露密钥、SQL 或内部文件路径。HTTP 约定：
401 未认证；403 无权限；404 资源不存在或按策略隐藏；409 幂等/状态冲突；
413 文件超限；422 参数错误；429 限流；503 模型或检索暂不可用。
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import get_request_id

logger = logging.getLogger(__name__)


class AppError(Exception):
    status_code: int = 500
    code: str = "internal_error"
    default_message: str = "服务内部错误"
    retryable: bool = False

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        status_code: int | None = None,
        retryable: bool | None = None,
    ) -> None:
        self.message = message or self.default_message
        self.code = code or self.code
        self.status_code = status_code or self.status_code
        self.retryable = self.retryable if retryable is None else retryable
        super().__init__(self.message)

    def to_body(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "request_id": get_request_id(),
            "retryable": self.retryable,
        }


class Unauthorized(AppError):
    status_code = 401
    code = "unauthorized"
    default_message = "未认证"


class Forbidden(AppError):
    status_code = 403
    code = "forbidden"
    default_message = "无权访问该资源"


class NotFound(AppError):
    status_code = 404
    code = "not_found"
    default_message = "资源不存在"


class Conflict(AppError):
    status_code = 409
    code = "conflict"
    default_message = "状态冲突"


class PayloadTooLarge(AppError):
    status_code = 413
    code = "payload_too_large"
    default_message = "文件超出大小限制"


class ValidationFailed(AppError):
    status_code = 422
    code = "validation_failed"
    default_message = "请求参数错误"


class RateLimited(AppError):
    status_code = 429
    code = "rate_limited"
    default_message = "请求过于频繁"
    retryable = True


class ServiceUnavailable(AppError):
    status_code = 503
    code = "service_unavailable"
    default_message = "服务暂不可用"
    retryable = True


class UnsupportedFormat(AppError):
    status_code = 422
    code = "unsupported_format"
    default_message = "不支持的文件格式"


class NeedsReview(AppError):
    """文件可解析但质量不足：转 needs_review，不发布索引（实现文档 §4.1）。"""

    status_code = 422
    code = "needs_review"
    default_message = "文件解析质量不足"


def _error_response(status_code: int, body: dict) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=body)


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def handle_app_error(_: Request, exc: AppError) -> JSONResponse:
        return _error_response(exc.status_code, exc.to_body())

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        location = ".".join(str(part) for part in first.get("loc", []))
        detail = first.get("msg", "invalid")
        message = f"参数错误：{location} {detail}" if location else "请求参数错误"
        return _error_response(
            422,
            AppError(message, code="validation_failed", status_code=422).to_body(),
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return _error_response(
            exc.status_code,
            AppError(str(exc.detail), code="http_error", status_code=exc.status_code).to_body(),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled exception: %s", exc)
        return _error_response(500, AppError().to_body())
