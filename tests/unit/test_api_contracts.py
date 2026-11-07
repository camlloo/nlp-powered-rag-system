"""前端历史轮数与注册字段的回归契约。"""

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api import deps, qa
from app.api.auth import RegisterRequest
from app.core.auth import get_admin_principal
from app.core.config import get_settings
from app.core.errors import Conflict
from app.main import app
from app.schemas.qa import QaRequestModel
from app.schemas.retrieval import RetrievalDebugRequest


@pytest.mark.parametrize("model", [QaRequestModel, RetrievalDebugRequest])
def test_history_accepts_two_messages_per_round(model):
    history = [
        {"role": role, "content": "历史内容"}
        for _ in range(get_settings().history_max_rounds)
        for role in ("user", "assistant")
    ]
    assert model(kb_id=1, question="问题", history=history).history
    with pytest.raises(ValidationError):
        model(kb_id=1, question="问题", history=history + history[:1])


def test_registration_normalizes_duplicate_org_tags():
    payload = RegisterRequest(
        username="tester", password="password", display_name="测试",
        org_tags=[" 研发部 ", "研发部", " ", "市场部"],
    )
    assert payload.org_tags == ["研发部", "市场部"]


def test_registration_rejects_oversized_org_tags():
    with pytest.raises(ValidationError):
        RegisterRequest(
            username="tester", password="password", display_name="测试", org_tags=["部" * 65],
        )


def test_stream_keeps_business_error_code_and_message(monkeypatch):
    from types import SimpleNamespace

    async def stream_ask(**kwargs):
        yield {"event": "meta", "data": {"request_id": "qa_test"}}
        raise Conflict("知识库状态已变化，请重新提问")

    monkeypatch.setattr(qa, "_build_qa_service", lambda: SimpleNamespace(stream_ask=stream_ask))
    monkeypatch.setattr(qa, "require_kb_access", lambda **kwargs: None)
    app.dependency_overrides[deps._current_principal] = get_admin_principal
    try:
        response = TestClient(app).post(
            f"{get_settings().api_prefix}/qa/stream", json={"kb_id": 1, "question": "问题"},
        )
    finally:
        app.dependency_overrides.pop(deps._current_principal, None)
    assert response.status_code == 200
    assert "event: error" in response.text
    assert '"code": "conflict"' in response.text
    assert '"message": "知识库状态已变化，请重新提问"' in response.text
