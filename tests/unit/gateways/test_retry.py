"""重试策略单测：分类、Retry-After、退避抖动边界、非重试错误立即失败。"""

import asyncio

from app.core.config import Settings
from app.gateways.errors import ModelResponseInvalid
from app.gateways.retry import classify_error, with_retries
from tests.unit.gateways.gateway_helpers import FakeAPIError


def test_classify_rate_limit_with_retry_after():
    cls = classify_error(FakeAPIError(429, {"retry-after": "7"}))
    assert cls.retryable is True
    assert cls.retry_after == 7.0
    assert cls.status == 429


def test_classify_rate_limit_without_header():
    assert classify_error(FakeAPIError(429)).retry_after is None


def test_classify_auth_and_param_errors_not_retryable():
    assert classify_error(FakeAPIError(401)).retryable is False
    assert classify_error(FakeAPIError(400)).retryable is False
    assert classify_error(FakeAPIError(403)).retryable is False


def test_classify_server_error_retryable():
    assert classify_error(FakeAPIError(500)).retryable is True
    assert classify_error(FakeAPIError(503)).retryable is True


def test_classify_connection_like_retryable():
    assert classify_error(RuntimeError("connection reset")).retryable is True


def test_classify_gateway_error_uses_own_flag():
    assert classify_error(ModelResponseInvalid()).retryable is False


def test_retry_succeeds_after_rate_limits():
    settings = Settings(retry_max_attempts=3)
    delays: list[float] = []

    async def sleeper(delay):
        delays.append(delay)

    state = {"n": 0}

    async def func():
        state["n"] += 1
        if state["n"] < 3:
            raise FakeAPIError(429, {"retry-after": "0.01"})
        return "ok"

    result = asyncio.run(with_retries(func, settings=settings, sleeper=sleeper))
    assert result == "ok"
    assert state["n"] == 3
    assert len(delays) == 2


def test_retry_exhausted_raises_last_error():
    settings = Settings(retry_max_attempts=2)
    attempts = {"n": 0}

    async def func():
        attempts["n"] += 1
        raise FakeAPIError(500)

    async def main():
        try:
            await with_retries(func, settings=settings, sleeper=_noop)
        except FakeAPIError:
            return True
        return False

    assert asyncio.run(main()) is True
    assert attempts["n"] == 2  # 最多 attempts 次


def test_non_retryable_fails_immediately():
    settings = Settings(retry_max_attempts=5)
    attempts = {"n": 0}

    async def func():
        attempts["n"] += 1
        raise FakeAPIError(401)

    async def main():
        try:
            await with_retries(func, settings=settings, sleeper=_noop)
        except FakeAPIError:
            return True
        return False

    assert asyncio.run(main()) is True
    assert attempts["n"] == 1  # 未重试


def test_backoff_respects_caps():
    settings = Settings(
        retry_max_attempts=4, retry_base_delay_seconds=0.5, retry_max_delay_seconds=8.0
    )
    delays: list[float] = []

    async def sleeper(delay):
        delays.append(delay)

    async def func():
        raise FakeAPIError(500)

    async def main():
        try:
            await with_retries(func, settings=settings, sleeper=sleeper)
        except FakeAPIError:
            pass

    asyncio.run(main())
    caps = [0.5, 1.0, 2.0]  # 指数退避上限：min(max_delay, base * 2**attempt)
    assert len(delays) == 3
    for delay, cap in zip(delays, caps, strict=True):
        assert 0 <= delay <= cap


async def _noop(_: float) -> None:
    return None
