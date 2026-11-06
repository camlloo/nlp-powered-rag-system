"""统一错误结构单测。"""

from app.core.errors import NotFound, RateLimited, ServiceUnavailable


def test_not_found_defaults():
    err = NotFound()
    assert err.status_code == 404
    assert err.code == "not_found"
    assert err.retryable is False
    assert err.to_body()["request_id"]


def test_retryable_flags():
    assert RateLimited().retryable is True
    assert ServiceUnavailable().retryable is True
