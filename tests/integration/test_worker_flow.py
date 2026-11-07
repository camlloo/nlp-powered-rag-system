"""worker 流程集成测试：构建、发布、租约、恢复（实现文档 §5.2/§9.4）。

无数据库时自动跳过（tests/integration/conftest.py 的 pg_ready 夹具）。
"""

import pytest
from sqlalchemy import func, select, update

from app.core.config import Settings
from app.core.errors import AppError
from app.db.models import IngestJob, KbChunk, KbCollection, KbDocumentVersion, KbEmbedding, KbIndex
from app.db.session import SessionLocal
from app.gateways.mock import MockEmbeddingGateway
from app.indexing.snapshot import IndexBuilder, RevisionChanged
from app.workers.runner import (
    claim_next_job,
    execute_claimed,
    finish_job,
    recover_stuck_jobs,
)
from tests.integration.helpers import cleanup_kb, create_kb

FILE1 = "第三章 费用报销。员工应在出差结束后 30 日内申请。交通费凭有效票据报销。".encode()
FILE2 = "第四章 考勤。员工应当在工作日 09:00 前签到。迟到按旷工处理。".encode()


@pytest.fixture
def env(pg_ready, test_settings):
    kb_id = create_kb()
    settings = test_settings
    builder = IndexBuilder(
        SessionLocal, MockEmbeddingGateway(dim=1024), settings, worker_id="test-worker"
    )
    yield kb_id, settings, builder
    cleanup_kb(kb_id)


def upload_file(kb_id: int, settings: Settings, name: str, content: bytes) -> int:
    import hashlib

    from app.parsers.intake import MinioStorage
    from app.storage import build_object_key

    uri = MinioStorage(settings).put(build_object_key(1, name), content)
    digest = hashlib.sha256(content).digest()
    with SessionLocal() as session:
        from app.db.models import KbDocument

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
        kb.data_revision += 1  # 与 API 上传行为一致：新文件入清单即递增修订号
        session.commit()
        return version.id


def create_job(kb_id: int, doc_version_id: int | None, job_type: str = "ingest_file") -> int:
    with SessionLocal() as session:
        job = IngestJob(
            kb_id=kb_id, doc_version_id=doc_version_id, job_type=job_type, payload_json={}
        )
        session.add(job)
        session.commit()
        return job.id


def get_kb(kb_id: int) -> KbCollection:
    with SessionLocal() as session:
        kb = session.get(KbCollection, kb_id)
        session.expunge(kb)
        return kb


def count_chunks(index_id: int) -> int:
    with SessionLocal() as session:
        return session.execute(
            select(func.count()).select_from(KbChunk).where(KbChunk.index_id == index_id)
        ).scalar_one()


def get_job_row(job_id: int) -> IngestJob:
    with SessionLocal() as session:
        job = session.get(IngestJob, job_id)
        session.expunge(job)
        return job


def test_build_publishes_snapshot(env):
    kb_id, settings, builder = env
    dvid = upload_file(kb_id, settings, "报销制度.txt", FILE1)
    report = run_build(builder, kb_id, dvid)

    kb = get_kb(kb_id)
    assert kb.active_index_id == report.index_id
    index = get_index(report.index_id)
    assert index.status == "ready"
    assert index.source_revision == 2  # 上传和解析状态变化分别递增
    assert count_chunks(report.index_id) == report.chunks
    assert report.chunks > 0
    with SessionLocal() as session:
        status = session.get(KbDocumentVersion, dvid).parse_status
        embedding_count = session.execute(
            select(func.count())
            .select_from(KbEmbedding)
            .join(KbChunk, KbChunk.id == KbEmbedding.chunk_id)
            .where(KbChunk.index_id == report.index_id)
        ).scalar_one()
    assert status == "succeeded"
    assert embedding_count == report.chunks  # 向量数与块数一致


def test_second_upload_rebuilds_full_snapshot(env):
    kb_id, settings, builder = env
    dvid1 = upload_file(kb_id, settings, "a.txt", FILE1)
    first = run_build(builder, kb_id, dvid1)
    dvid2 = upload_file(kb_id, settings, "b.txt", FILE2)
    second = run_build(builder, kb_id, dvid2)

    kb = get_kb(kb_id)
    assert kb.active_index_id == second.index_id != first.index_id
    # 全量重建：新快照包含两个文件的全部块；旧快照保留（ready 状态由首版继承）
    assert count_chunks(second.index_id) == count_chunks(first.index_id) + report_chunks(FILE2)
    with SessionLocal() as session:
        first_index = session.get(KbIndex, first.index_id)
        assert first_index.status == "ready"  # 旧索引保留，物理清理按策略异步执行


def test_needs_review_excluded_from_manifest(env, fake_minio):
    kb_id, settings, builder = env
    from tests.integration.helpers import build_pdf

    # 文本密度过低的 PDF：解析成功但质量分不足 -> needs_review，不进入索引
    thin_pdf = build_pdf(["ok"])
    uri, digest = fake_minio.seed(settings, "扫描件.pdf", thin_pdf)
    with SessionLocal() as session:
        from app.db.models import KbDocument

        document = KbDocument(kb_id=kb_id, title="扫描件")
        session.add(document)
        session.flush()
        version = KbDocumentVersion(
            kb_id=kb_id,
            document_id=document.id,
            version_no=1,
            title="扫描件",
            file_sha256=digest,
            storage_uri=uri,
            parse_status="pending",
        )
        session.add(version)
        kb = session.get(KbCollection, kb_id)
        kb.data_revision += 1
        session.commit()
        dvid = version.id

    report = run_build(builder, kb_id, dvid)
    kb = get_kb(kb_id)
    assert kb.active_index_id == report.index_id
    assert count_chunks(report.index_id) == 0  # needs_review 不进入索引
    with SessionLocal() as session:
        assert session.get(KbDocumentVersion, dvid).parse_status == "needs_review"


def test_parse_failed_aborts_build_keeps_old_index(env):
    kb_id, settings, builder = env
    good = upload_file(kb_id, settings, "good.txt", FILE1)
    run_build(builder, kb_id, good)
    kb_before = get_kb(kb_id)

    # UTF-8 与 GB18030 均不可解码
    bad = upload_file(kb_id, settings, "bad.txt", b"\xff\xff\xff\xff")
    with pytest.raises(AppError, match="解析失败"):
        run_build(builder, kb_id, bad)

    kb_after = get_kb(kb_id)
    assert kb_after.active_index_id == kb_before.active_index_id  # 旧索引持续服务
    with SessionLocal() as session:
        assert session.get(KbDocumentVersion, bad).parse_status == "failed"
        buildings = session.execute(
            select(func.count()).select_from(KbIndex).where(
                KbIndex.kb_id == kb_id, KbIndex.status == "building"
            )
        ).scalar_one()
    assert buildings == 0  # 失败发生在建快照行之前


def test_publish_revision_conflict_marks_superseded(env):
    kb_id, settings, builder = env
    with SessionLocal() as session:
        kb = session.get(KbCollection, kb_id)
        source_revision = kb.data_revision
    index_id = builder._create_index_row(kb_id, source_revision, [], tenant_id=1)
    with SessionLocal() as session:
        kb = session.get(KbCollection, kb_id)
        kb.data_revision += 1  # 构建期间有人上传/删除
        session.commit()

    with pytest.raises(RevisionChanged):
        builder._publish(kb_id, index_id, source_revision)
    assert get_index(index_id).status == "superseded"
    assert get_kb(kb_id).active_index_id is None


def test_claim_lease_and_owner_guard(env):
    kb_id, settings, builder = env
    dvid = upload_file(kb_id, settings, "a.txt", FILE1)
    job_id = create_job(kb_id, dvid)

    with SessionLocal() as session:
        job = claim_next_job(session, "worker-1", lease_seconds=300)
        assert job.id == job_id
        assert job.status == "running" and job.attempt == 1 and job.lease_owner == "worker-1"
        # 无其他可领任务
        assert claim_next_job(session, "worker-1", lease_seconds=300) is None

    # 租约过期后可被其他 worker 重新领取，attempt 递增
    with SessionLocal() as session:
        session.execute(
            update(IngestJob)
            .where(IngestJob.id == job_id)
            .values(lease_until=func.now() - func.make_interval(0, 0, 0, 0, 0, 0, 1))
        )
        session.commit()
    with SessionLocal() as session:
        job = claim_next_job(session, "worker-2", lease_seconds=300)
        assert job.attempt == 2 and job.lease_owner == "worker-2"

    # 旧 owner 不能结束任务；新 owner 可以
    assert finish_job(SessionLocal, job_id, "worker-1", status="succeeded") is False
    assert finish_job(SessionLocal, job_id, "worker-2", status="succeeded") is True
    assert get_job_row(job_id).status == "succeeded"


def test_recover_stuck_jobs_marks_failed(env):
    kb_id, settings, builder = env
    job_id = create_job(kb_id, None, job_type="reindex")
    with SessionLocal() as session:
        session.execute(
            update(IngestJob)
            .where(IngestJob.id == job_id)
            .values(
                status="running",
                attempt=IngestJob.max_attempt,
                lease_owner="dead-worker",
                lease_until=func.now() - func.make_interval(0, 0, 0, 0, 0, 0, 1),
            )
        )
        session.commit()
    with SessionLocal() as session:
        assert recover_stuck_jobs(session) == 1
    row = get_job_row(job_id)
    assert row.status == "failed" and row.error_code == "lease_expired"


def test_execute_claimed_end_to_end(env):
    kb_id, settings, builder = env
    dvid = upload_file(kb_id, settings, "a.txt", FILE1)
    job_id = create_job(kb_id, dvid)
    with SessionLocal() as session:
        job = claim_next_job(session, "test-worker", lease_seconds=300)
    import asyncio

    asyncio.run(
        execute_claimed(
            job,
            builder=builder,
            session_factory=SessionLocal,
            worker_id="test-worker",
            lease_seconds=300,
        )
    )
    assert get_job_row(job_id).status == "succeeded"
    assert get_kb(kb_id).active_index_id is not None


def test_reindex_job_type(env):
    kb_id, settings, builder = env
    dvid = upload_file(kb_id, settings, "a.txt", FILE1)
    run_build(builder, kb_id, dvid)
    job_id = create_job(kb_id, None, job_type="reindex")
    with SessionLocal() as session:
        job = claim_next_job(session, "test-worker", lease_seconds=300)
    import asyncio

    asyncio.run(
        execute_claimed(
            job,
            builder=builder,
            session_factory=SessionLocal,
            worker_id="test-worker",
            lease_seconds=300,
        )
    )
    assert get_job_row(job_id).status == "succeeded"
    active_id = get_kb(kb_id).active_index_id
    assert count_chunks(active_id) == count_chunks_of_first(kb_id)


# ---------------------------------------------------------------- helpers


def run_build(builder: IndexBuilder, kb_id: int, dvid: int | None):
    import asyncio

    return asyncio.run(builder.build(kb_id=kb_id, new_doc_version_id=dvid))


def get_index(index_id: int) -> KbIndex:
    with SessionLocal() as session:
        index = session.get(KbIndex, index_id)
        session.expunge(index)
        return index


def report_chunks(content: bytes) -> int:
    """估算单个文件的块数：直接跑一次流水线。"""
    from app.core.config import get_settings
    from app.nlp.pipeline import run_pipeline
    from app.parsers.intake import parse_upload

    settings = get_settings()
    parsed = parse_upload("estimate.txt", None, content, settings)
    return len(run_pipeline(parsed, settings).chunks)


def count_chunks_of_first(kb_id: int) -> int:
    with SessionLocal() as session:
        first = session.execute(
            select(KbIndex.id)
            .where(KbIndex.kb_id == kb_id, KbIndex.status == "ready")
            .order_by(KbIndex.id)
            .limit(1)
        ).scalar_one()
        return count_chunks(first)
