"""问答链路集成测试：answered / 拒答不调模型 / 校验失败 / 审计与引用接口。"""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.auth import get_admin_principal
from app.core.config import get_settings
from app.core.errors import Conflict
from app.db.models import QaCitation, QaFeedback, QaRequest
from app.db.session import SessionLocal
from app.gateways.mock import MockChatGateway, MockEmbeddingGateway
from app.generation.service import INSUFFICIENT_ANSWER, QaService
from app.indexing.cache import IndexCacheManager
from app.main import app
from app.retrieval.service import RetrievalService
from tests.integration.helpers import cleanup_kb, create_kb


def make_qa_service(settings, chat=None) -> QaService:
    retrieval = RetrievalService(
        IndexCacheManager(settings), MockEmbeddingGateway(dim=1024), settings
    )
    return QaService(retrieval, chat or MockChatGateway(), settings)


def test_qa_answered_with_citations_and_audit(indexed_kb):
    kb_id, settings = indexed_kb
    service = make_qa_service(settings)
    outcome = asyncio_run(
        service.ask(
            kb_id=kb_id, principal=get_admin_principal(), question="报销应该在多少天内申请？"
        )
    )
    assert outcome.status == "answered"
    assert "[S1]" in outcome.answer
    assert outcome.citations, "至少一条引用"
    citation = outcome.citations[0]
    assert citation.source_id == "S1"
    assert citation.citation_id and len(citation.citation_id) >= 16  # 不透明标识
    assert citation.document_title == "报销制度"
    assert citation.document_version == 1
    assert citation.locator["type"] == "txt"
    assert citation.quote

    with SessionLocal() as session:
        qa_request = session.execute(
            select(QaRequest).where(QaRequest.request_id == outcome.request_id)
        ).scalar_one()
        assert qa_request.answer_status == "answered"
        assert qa_request.usage_json["total_tokens"] > 0
        assert qa_request.latency_ms >= 0
        citations = session.execute(
            select(QaCitation).where(QaCitation.qa_request_id == qa_request.id)
        ).scalars().all()
    assert len(citations) == len(outcome.citations)


def test_qa_insufficient_skips_model(indexed_kb):
    kb_id, settings = indexed_kb
    empty_kb = create_kb()
    try:
        chat = MockChatGateway()
        service = make_qa_service(settings, chat)
        outcome = asyncio_run(
            service.ask(
                kb_id=empty_kb, principal=get_admin_principal(), question="报销"
            )
        )
        assert outcome.status == "insufficient_evidence"
        assert outcome.answer == INSUFFICIENT_ANSWER
        assert chat.calls == []  # 拒答路径绝不调用生成模型（§7.5）
        with SessionLocal() as session:
            qa_request = session.execute(
                select(QaRequest).where(QaRequest.request_id == outcome.request_id)
            ).scalar_one()
        assert qa_request.answer_status == "insufficient_evidence"
    finally:
        cleanup_kb(empty_kb)


def test_qa_validation_failed_shows_legal_sources(indexed_kb):
    kb_id, settings = indexed_kb
    chat = MockChatGateway(reply="我不知道，资料里没有写。")  # 无任何引用编号
    service = make_qa_service(settings, chat)
    outcome = asyncio_run(
        service.ask(kb_id=kb_id, principal=get_admin_principal(), question="报销时限")
    )
    assert outcome.status == "answer_validation_failed"
    assert outcome.answer is None  # 未校验答案不输出
    assert outcome.error_code == "missing_citation"
    assert len(chat.calls) == 2  # 首次 + 重生成一次（§8.3）
    assert outcome.citations  # 展示合法来源供用户查看


def test_qa_stale_index_conflict(indexed_kb):
    kb_id, settings = indexed_kb
    service = make_qa_service(settings)
    fake_result = SimpleNamespace(index_id=1, mode="hybrid")
    import time as time_module
    from uuid import uuid4

    request_id = f"qa_debug_{uuid4().hex[:8]}"
    with pytest.raises(Conflict, match="知识库状态已变化"):
        service._finish(
            request_id=request_id,
            status="answered",
            answer="答案 [S1]",
            result=fake_result,
            citations=[],
            usage=SimpleNamespace(prompt_tokens=0, total_tokens=0),
            started=time_module.monotonic(),
            kb_id=kb_id,
            tenant_id=1,
            user_id=1,
            question="问题",
            top_k=6,
            expected_state=(None, 0, 0),  # 与当前库内状态不一致 → 409
        )
    with SessionLocal() as session:
        qa_request = session.execute(
            select(QaRequest).where(QaRequest.request_id == request_id)
        ).scalar_one()
    assert qa_request.answer_status == "stale_index"  # 变更轨迹也入审计



def test_mock_stream_yields_chunks():
    gateway = MockChatGateway(reply="答案正文 [S1]")

    async def collect():
        pieces = []
        async for piece in gateway.generate_stream(system_prompt="s", user_payload="u"):
            pieces.append(piece)
        return pieces

    pieces = asyncio_run(collect())
    assert "".join(p["delta"] for p in pieces) == "答案正文 [S1]"
    assert pieces[-1]["finish_reason"] == "stop"


def test_qa_stream_endpoint_sse_sequence(indexed_kb):
    kb_id, _ = indexed_kb
    headers = {"X-API-Token": get_settings().api_token}
    prefix = get_settings().api_prefix
    with TestClient(app) as client:
        resp = client.post(
            f"{prefix}/qa/stream",
            json={"kb_id": kb_id, "question": "报销应该在多少天内申请？"},
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        body = resp.text

    # 事件顺序：meta → citations → delta* → done
    events = [line[7:] for line in body.splitlines() if line.startswith("event: ")]
    assert events[0] == "meta"
    assert "citations" in events
    assert "delta" in events
    assert events[-1] == "done"
    assert '"status": "answered"' in body
    assert body.count("event: delta") >= 3  # Mock 按 4 字符分片，必有多个 delta
    # 审计落库
    with SessionLocal() as session:
        row = session.execute(
            select(QaRequest).where(
                QaRequest.request_id.like("qa_%")
            ).order_by(QaRequest.id.desc()).limit(1)
        ).scalar_one()
    assert row.answer_status == "answered"
    assert "30 日内" in row.answer


def test_qa_stream_empty_kb_insufficient(indexed_kb):
    kb_id, _ = indexed_kb
    empty_kb = create_kb()
    try:
        headers = {"X-API-Token": get_settings().api_token}
        prefix = get_settings().api_prefix
        with TestClient(app) as client:
            resp = client.post(
                f"{prefix}/qa/stream",
                json={"kb_id": empty_kb, "question": "报销"},
                headers=headers,
            )
        body = resp.text
        assert "event: done" in body
        assert "insufficient_evidence" in body
        assert "event: delta" not in body  # 拒答路径不调生成模型
    finally:
        cleanup_kb(empty_kb)


def test_qa_stream_unauthorized(indexed_kb):
    kb_id, _ = indexed_kb
    prefix = get_settings().api_prefix
    with TestClient(app) as client:
        resp = client.post(
            f"{prefix}/qa/stream",
            json={"kb_id": kb_id, "question": "报销"},
        )
    assert resp.status_code == 401

def test_citations_and_feedback_api(indexed_kb):
    kb_id, settings = indexed_kb
    service = make_qa_service(settings)
    outcome = asyncio_run(
        service.ask(kb_id=kb_id, principal=get_admin_principal(), question="报销时限")
    )
    citation_id = outcome.citations[0].citation_id
    headers = {"X-API-Token": get_settings().api_token}
    prefix = get_settings().api_prefix

    with TestClient(app) as client:
        # 引用详情：重新授权后可查
        resp = client.get(f"{prefix}/citations/{citation_id}", headers=headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["document_title"] == "报销制度"
        assert body["quote"]
        assert body["locator"]["type"] == "txt"
        # 反馈：有帮助 + 原因
        resp = client.post(
            f"{prefix}/feedback",
            json={"request_id": outcome.request_id, "rating": "helpful", "reason": "引用准确"},
            headers=headers,
        )
        assert resp.status_code == 201
        with SessionLocal() as session:
            feedback = session.get(QaFeedback, resp.json()["feedback_id"])
        assert feedback.rating == "helpful" and feedback.reason == "引用准确"
        # 异常路径
        assert client.get(f"{prefix}/citations/does-not-exist", headers=headers).status_code == 404
        assert (
            client.post(
                f"{prefix}/feedback",
                json={"request_id": "no-such-request", "rating": "helpful"},
                headers=headers,
            ).status_code
            == 404
        )
        assert (
            client.post(
                f"{prefix}/feedback",
                json={"request_id": outcome.request_id, "rating": "maybe"},
                headers=headers,
            ).status_code
            == 422
        )
        assert client.get(f"{prefix}/citations/{citation_id}").status_code == 401


def test_qa_api_end_to_end(indexed_kb):
    kb_id, _ = indexed_kb
    headers = {"X-API-Token": get_settings().api_token}
    prefix = get_settings().api_prefix
    with TestClient(app) as client:
        resp = client.post(
            f"{prefix}/qa",
            json={"kb_id": kb_id, "question": "报销应该在多少天内申请？"},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "answered"
        assert "[S1]" in body["answer"]
        assert body["retrieval_mode"] == "hybrid"
        assert body["citations"][0]["document_version"] == 1
        unauth = client.post(
            f"{prefix}/qa", json={"kb_id": kb_id, "question": "报销"}
        )
        assert unauth.status_code == 401


def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)
