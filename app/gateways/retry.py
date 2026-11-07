"""统一重试策略（实现文档 §6.2）。

SDK 以 max_retries=0 运行，重试全部由网关控制：
- 429 尊重 Retry-After；
- 网络错误与可重试 5xx 使用指数退避加随机抖动，最多 retry_max_attempts 次；
- 认证/参数/权限错误（4xx 除 429）以及协议校验类错误立即失败。
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass

from app.core.config import Settings


@dataclass(frozen=True)
class ErrorClass:
    retryable: bool
    retry_after: float | None = None
    status: int | None = None


def classify_error(exc: BaseException) -> ErrorClass:
    """按异常特征分类：兼容 openai 异常（status_code/response 属性）与网关错误。

    网关自身的 AppError 带有 retryable=False 的语义，直接采纳；
    无状态码的异常（超时、连接类）按可重试处理。
    """
    retryable_flag = getattr(exc, "retryable", None)
    if retryable_flag is False:
        status = getattr(exc, "status_code", None)
        return ErrorClass(False, None, status if isinstance(status, int) else None)

    status = getattr(exc, "status_code", None)
    if isinstance(status, bool):
        status = None
    if isinstance(status, int):
        if status == 429:
            return ErrorClass(True, _retry_after(exc), 429)
        if 400 <= status < 500:
            return ErrorClass(False, None, status)
        if status >= 500:
            return ErrorClass(True, None, status)
    return ErrorClass(True, None, None)


def _retry_after(exc: BaseException) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    raw = None
    if headers is not None:
        try:
            raw = headers.get("retry-after")
        except AttributeError:
            raw = None
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return None


async def with_retries(
    func, *, settings: Settings, sleeper=asyncio.sleep
):
    """执行异步零参调用，按策略重试；重试耗尽后抛出最后一次异常。"""

    attempts = max(1, settings.retry_max_attempts)
    for attempt in range(attempts):
        try:
            return await func()
        except Exception as exc:  # noqa: BLE001  统一分类后决定重试或抛出
            cls = classify_error(exc)
            if not cls.retryable or attempt == attempts - 1:
                raise
            if cls.retry_after is not None:
                delay = cls.retry_after
            else:
                cap = min(
                    settings.retry_max_delay_seconds,
                    settings.retry_base_delay_seconds * (2**attempt),
                )
                delay = random.uniform(0, cap)
            await sleeper(delay)
    raise AssertionError("unreachable")
