"""评测 harness 集成测试：真实建索引 → 跑种子评测集 → 指标报告与消融。

种子集用于验证 harness 与回归；正式验收指标须以真实语料扩展至 ≥200 题
人工标注后跑分（实现文档 §14.1）。
"""

import asyncio

import pytest

from app.core.auth import get_admin_principal
from app.db.models import KbCollection, KbDocument, KbDocumentVersion
from app.db.session import SessionLocal
from app.gateways.mock import MockChatGateway, MockEmbeddingGateway
from app.generation.service import QaService
from app.indexing.cache import IndexCacheManager
from app.indexing.snapshot import IndexBuilder
from app.retrieval.service import RetrievalService
from tests.evaluation.ablation import channel_ablation, chunking_ablation
from tests.evaluation.dataset import load_cases, validate_dataset
from tests.evaluation.metrics import CaseResult, aggregate, format_report
from tests.evaluation.runner import run_evaluation
from tests.integration.helpers import cleanup_kb, create_kb

CORPUS = [
    ("报销制度.txt", "第三章 费用报销。员工应在出差结束后 30 日内申请。交通费凭有效票据报销。"),
    ("考勤制度.txt", "第四章 考勤。员工应当在工作日 09:00 前签到。"
                  "迟到 30 分钟以上按旷工半日处理。"),
    ("信息安全.txt", "第五章 信息安全。禁止将内部资料上传至外部网盘。"
                  "办公系统密码每 90 天更换一次。"),
    ("采购流程.txt", "第六章 采购。单笔金额超过 5000 元的采购须三家比价。"
                  "紧急采购可事后补审批。"),
    ("版本发布.txt", "API 网关 v2.1.3 的单接口限额为 500 元。"
                  "API 网关 v2.0.9 的单接口限额为 300 元。"),
    ("差旅补贴.txt", "出差期间餐费补贴为每天 100 元。"
                  "市内交通费实报实销，无票据不予报销。"),
]


@pytest.fixture
def eval_kb(pg_ready, test_settings, fake_minio):
    kb_id = create_kb()
    settings = test_settings
    builder = IndexBuilder(
        SessionLocal, MockEmbeddingGateway(dim=1024), settings, worker_id="eval-worker"
    )
    for name, content in CORPUS:
        uri, digest = fake_minio.seed(settings, name, content.encode())
        with SessionLocal() as session:
            document = KbDocument(kb_id=kb_id, title=name.rsplit(".", 1)[0])
            session.add(document)
            session.flush()
            version = KbDocumentVersion(
                kb_id=kb_id,
                document_id=document.id,
                version_no=1,
                title=document.title,
                file_sha256=digest,
                storage_uri=uri,
                parse_status="pending",
            )
            session.add(version)
            kb = session.get(KbCollection, kb_id)
            kb.data_revision += 1
            session.commit()
            doc_version_id = version.id
        asyncio.run(builder.build(kb_id=kb_id, new_doc_version_id=doc_version_id))
    yield kb_id, settings
    cleanup_kb(kb_id)


def test_seed_dataset_valid():
    cases = load_cases()
    assert len(cases) >= 20
    assert sum(1 for c in cases if not c.answerable) >= 5
    assert sum(1 for c in cases if "version" in c.tags) >= 3
    assert validate_dataset(cases) == []


def test_metrics_pure_functions():
    results = [
        CaseResult(
            "a1", True, "ok", 10,
            relevant_total=2, relevant_in_top5=2, first_relevant_rank_10=1,
        ),
        CaseResult(
            "a2", True, "ok", 20,
            relevant_total=1, relevant_in_top5=0, first_relevant_rank_10=0,
        ),
        CaseResult("u1", False, "insufficient_evidence", 5),
        CaseResult("u2", False, "ok", 5),
    ]
    agg = aggregate(results)
    assert agg["sample_size"] == 4 and agg["n_answerable"] == 2
    assert agg["recall_at_5"] == pytest.approx(0.5)
    assert agg["hit_at_5"] == pytest.approx(0.5)
    assert agg["mrr_at_10"] == pytest.approx(0.5)
    assert agg["rejection_rate"] == pytest.approx(0.5)
    assert agg["false_rejection_rate"] == pytest.approx(0.0)


def test_evaluation_run_report(eval_kb):
    kb_id, settings = eval_kb
    cases = load_cases()
    cache = IndexCacheManager(settings)
    service = RetrievalService(cache, MockEmbeddingGateway(dim=1024), settings)

    results = asyncio.run(run_evaluation(service, cases, kb_id=kb_id, tenant_id=1))
    assert len(results) == len(cases)
    loaded = cache.get(kb_id)
    assert loaded is not None
    # 重新带全库相关计数跑一遍以计算 Recall 分母
    results = asyncio.run(
        run_evaluation(service, cases, kb_id=kb_id, tenant_id=1, loaded=loaded)
    )
    agg = aggregate(results)
    assert agg["sample_size"] == len(cases)
    assert agg["recall_denominator"] == agg["n_answerable"]
    # 词项通道在种子集上应具备基础命中率（mock 向量无语义，仅词项通道可靠）
    assert agg["hit_at_5"] is not None and agg["hit_at_5"] >= 0.5
    print(format_report("hybrid", agg))


def test_channel_ablation(eval_kb):
    kb_id, settings = eval_kb
    cache = IndexCacheManager(settings)
    embedding = MockEmbeddingGateway(dim=1024)
    service = RetrievalService(cache, embedding, settings)
    asyncio.run(service.retrieve(kb_id=kb_id, tenant_id=1, question="报销"))  # 触发装载
    loaded = cache.get(kb_id)

    report = asyncio.run(
        channel_ablation(loaded, load_cases(), embedding=embedding, settings=settings)
    )
    assert set(report.keys()) == {"lexical", "vector", "hybrid", "hybrid_rerank"}
    n_answerable = sum(1 for c in load_cases() if c.answerable)
    for stats in report.values():
        assert stats["n"] == n_answerable
        assert stats["hit_at_5"] is not None
    print("通道消融：", report)


def test_chunking_ablation(eval_kb):
    kb_id, settings = eval_kb
    cache = IndexCacheManager(settings)
    service = RetrievalService(cache, MockEmbeddingGateway(dim=1024), settings)
    asyncio.run(service.retrieve(kb_id=kb_id, tenant_id=1, question="报销"))
    loaded = cache.get(kb_id)
    rule_chunks = [record.content for record in loaded.records.values()]
    documents = [content for _, content in CORPUS]

    report = chunking_ablation(rule_chunks, documents, load_cases())
    assert set(report.keys()) == {"rule_chunking", "fixed_window"}
    assert report["rule_chunking"]["hit_at_5"] is not None
    print("切块消融：", report)


def test_qa_end_to_end_on_eval_corpus(eval_kb):
    """评测语料上跑一次完整问答：拒答与引用行为可观测。"""
    kb_id, settings = eval_kb
    retrieval = RetrievalService(
        IndexCacheManager(settings), MockEmbeddingGateway(dim=1024), settings
    )
    chat = MockChatGateway()
    service = QaService(retrieval, chat, settings)
    answered = asyncio.run(
        service.ask(
            kb_id=kb_id, principal=get_admin_principal(), question="报销应该在多少天内申请？"
        )
    )
    assert answered.status == "answered"
    empty_kb = create_kb()
    try:
        rejected = asyncio.run(
            service.ask(kb_id=empty_kb, principal=get_admin_principal(), question="年假有多少天？")
        )
        assert rejected.status == "insufficient_evidence"
        assert chat.calls == [] or all(
            "年假" not in call["user_payload"] for call in chat.calls
        )
    finally:
        cleanup_kb(empty_kb)
