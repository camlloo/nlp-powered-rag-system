"""检索服务集成测试：真实建索引后走完整检索链路与 debug 接口。"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.db.models import KbCollection, KbDocument, KbDocumentVersion
from app.db.session import SessionLocal
from app.gateways.mock import MockEmbeddingGateway
from app.indexing.cache import IndexCacheManager
from app.indexing.snapshot import IndexBuilder
from app.main import app
from app.parsers.intake import save_upload
from app.retrieval.service import RetrievalService
from tests.integration.helpers import cleanup_kb, create_kb

FILE1 = "第三章 费用报销。员工应在出差结束后 30 日内申请。交通费凭有效票据报销。".encode()
FILE2 = "第四章 考勤。员工应当在工作日 09:00 前签到。迟到按旷工处理。".encode()


@pytest.fixture
def indexed_kb(pg_ready, tmp_path):
    """建库 + 上传两个文件 + 真实 builder 构建快照。"""
    kb_id = create_kb()
    settings = Settings(storage_dir=str(tmp_path))
    builder = IndexBuilder(
        SessionLocal, MockEmbeddingGateway(dim=1024), settings, worker_id="ret-worker"
    )
    for name, content in (("报销制度.txt", FILE1), ("考勤制度.txt", FILE2)):
        uri, sha_hex = save_upload(content, "txt", upload_dir=settings.storage_dir + "/uploads")
        with SessionLocal() as session:
            document = KbDocument(kb_id=kb_id, title=name.rsplit(".", 1)[0])
            session.add(document)
            session.flush()
            version = KbDocumentVersion(
                kb_id=kb_id,
                document_id=document.id,
                version_no=1,
                title=document.title,
                file_sha256=bytes.fromhex(sha_hex),
                storage_uri=uri,
                parse_status="pending",
            )
            session.add(version)
            kb = session.get(KbCollection, kb_id)
            kb.data_revision += 1
            session.commit()
            dvid = version.id
        asyncio.run(builder.build(kb_id=kb_id, new_doc_version_id=dvid))
    yield kb_id, settings
    cleanup_kb(kb_id)


def make_service(settings: Settings) -> tuple[RetrievalService, IndexCacheManager]:
    cache = IndexCacheManager(settings)
    service = RetrievalService(cache, MockEmbeddingGateway(dim=1024), settings)
    return service, cache


def test_retrieve_hybrid_structure_and_determinism(indexed_kb):
    kb_id, settings = indexed_kb
    service, _ = make_service(settings)
    result = asyncio.run(
        service.retrieve(kb_id=kb_id, tenant_id=1, question="报销应该在多少天内申请？")
    )
    assert result.status == "ok"
    assert result.mode == "hybrid"
    assert result.index_id is not None
    # 词项通道命中"报销"所在块
    assert result.lexical, "BM25 应有命中"
    # 向量通道独立返回（Mock 向量非语义，但通道必须独立产出）
    assert result.vector
    assert result.fused and result.reranked
    assert 1 <= len(result.evidence) <= 6
    evidence_block = result.evidence[0]
    assert evidence_block.content
    assert evidence_block.source_locator["type"] == "txt"
    # 确定性：相同输入两次检索结果完全一致
    again = asyncio.run(
        service.retrieve(kb_id=kb_id, tenant_id=1, question="报销应该在多少天内申请？")
    )
    assert [e.chunk_id for e in again.evidence] == [e.chunk_id for e in result.evidence]


def test_lexical_channel_relevance(indexed_kb):
    kb_id, settings = indexed_kb
    service, _ = make_service(settings)
    result = asyncio.run(service.retrieve(kb_id=kb_id, tenant_id=1, question="考勤签到规定"))
    top_lexical_ids = [h.chunk_id for h in result.lexical[:2]]
    assert top_lexical_ids
    # 直接核验：命中的块 search_text 含查询词
    result2 = asyncio.run(service.retrieve(kb_id=kb_id, tenant_id=1, question="迟到怎么处理？"))
    assert any("迟到" in e.content for e in result2.evidence[:3])


def test_lexical_only_degradation(indexed_kb):
    kb_id, settings = indexed_kb
    cache = IndexCacheManager(settings)
    gateway = MockEmbeddingGateway(dim=1024)
    from app.gateways.errors import EmbeddingUnavailable

    gateway.failures.append(EmbeddingUnavailable("查询向量超时"))
    service = RetrievalService(cache, gateway, settings)
    result = asyncio.run(service.retrieve(kb_id=kb_id, tenant_id=1, question="报销时限"))
    assert result.mode == "lexical_only"
    assert result.status == "ok"
    assert result.vector == []
    assert result.lexical  # 词项通道独立支撑召回


def test_empty_kb_insufficient(indexed_kb):
    _, settings = indexed_kb
    empty_kb = create_kb()
    try:
        service, _ = make_service(settings)
        result = asyncio.run(service.retrieve(kb_id=empty_kb, tenant_id=1, question="报销"))
        assert result.status == "insufficient_evidence"
        assert result.evidence == []
    finally:
        cleanup_kb(empty_kb)


def test_clarification_required(indexed_kb):
    kb_id, settings = indexed_kb
    service, _ = make_service(settings)
    result = asyncio.run(
        service.retrieve(
            kb_id=kb_id,
            tenant_id=1,
            question="它到底怎么规定？",
            history=[{"role": "assistant", "content": "v2.1.3 限额 500元，2026年11月7日 生效。"}],
        )
    )
    assert result.status == "clarification_required"
    assert result.evidence == []


def test_debug_api_end_to_end(indexed_kb, tmp_path):
    kb_id, _ = indexed_kb
    test_settings = Settings(storage_dir=str(tmp_path))
    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        with TestClient(app) as client:
            headers = {"X-API-Token": get_settings().api_token}
            resp = client.post(
                f"{get_settings().api_prefix}/retrieval/debug",
                json={"kb_id": kb_id, "question": "报销应该在多少天内申请？"},
                headers=headers,
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["status"] == "ok"
            assert body["mode"] == "hybrid"
            assert body["lexical"] and body["vector"] and body["fused"]
            assert body["evidence"]
            assert "top_cosine" in body["gate"]

            # 参数校验：问题限长 / top_k 边界 → 422
            long_question = "问" * 2001
            resp = client.post(
                f"{get_settings().api_prefix}/retrieval/debug",
                json={"kb_id": kb_id, "question": long_question},
                headers=headers,
            )
            assert resp.status_code == 422
            resp = client.post(
                f"{get_settings().api_prefix}/retrieval/debug",
                json={"kb_id": kb_id, "question": "报销", "top_k": 11},
                headers=headers,
            )
            assert resp.status_code == 422
            # 未认证 401
            resp = client.post(
                f"{get_settings().api_prefix}/retrieval/debug",
                json={"kb_id": kb_id, "question": "报销"},
            )
            assert resp.status_code == 401
    finally:
        app.dependency_overrides.pop(get_settings, None)
