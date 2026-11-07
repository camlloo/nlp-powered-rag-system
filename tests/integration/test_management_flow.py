"""管理接口集成测试：文档列表、逻辑删除即时失效、重复上传、物理清理。

物理清理用例如实反映 upload_object_guard 协议：夹具登记 ready 屏障的
协议内对象会被真正清除；未登记屏障的遗留对象只保守保留并计数。
"""

import asyncio
import datetime as dt
import hashlib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.core.config import Settings, get_settings
from app.core.errors import Conflict
from app.db.models import (
    IngestJob,
    KbChunk,
    KbCollection,
    KbDocument,
    KbDocumentVersion,
    KbIndex,
)
from app.db.session import SessionLocal
from app.db.upload_guard import upload_object_guard
from app.gateways.mock import MockEmbeddingGateway
from app.indexing import cleanup
from app.indexing.cache import IndexCacheManager
from app.indexing.cleanup import cleanup_kb_scope
from app.indexing.snapshot import IndexBuilder
from app.main import app
from app.retrieval.service import RetrievalService
from app.workers.runner import claim_next_job, execute_claimed
from tests.integration.helpers import cleanup_kb, create_kb, seed_object_guard

FILE1 = "第三章 费用报销。员工应在出差结束后 30 日内申请。交通费凭有效票据报销。".encode()
FILE2 = "第四章 考勤。员工应当在工作日 09:00 前签到。迟到按旷工处理。".encode()


@pytest.fixture
def indexed_kb(pg_ready, test_settings, fake_minio):
    kb_id = create_kb()
    settings = test_settings
    builder = IndexBuilder(
        SessionLocal, MockEmbeddingGateway(dim=1024), settings, worker_id="mgmt-worker"
    )
    for name, content in (("报销制度.txt", FILE1), ("考勤制度.txt", FILE2)):
        uri, digest = fake_minio.seed(settings, name, content)
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
            seed_object_guard(session, uri)
            kb = session.get(KbCollection, kb_id)
            kb.data_revision += 1
            session.commit()
            doc_version_id = version.id
        asyncio.run(builder.build(kb_id=kb_id, new_doc_version_id=doc_version_id))
    yield kb_id, settings, builder
    cleanup_kb(kb_id)


def test_document_list_with_status(indexed_kb):
    kb_id, settings, builder = indexed_kb
    headers = {"X-API-Token": get_settings().api_token}
    prefix = get_settings().api_prefix
    with TestClient(app) as client:
        resp = client.get(f"{prefix}/kbs/{kb_id}/documents", headers=headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 2
        titles = {item["title"] for item in body["items"]}
        assert titles == {"报销制度", "考勤制度"}
        assert all(item["parse_status"] == "succeeded" for item in body["items"])
        assert client.get(
            f"{prefix}/kbs/{kb_id}/documents", headers={"X-API-Token": "wrong"}
        ).status_code == 401


def test_list_kbs(indexed_kb):
    kb_id, *_ = indexed_kb
    headers = {"X-API-Token": get_settings().api_token}
    prefix = get_settings().api_prefix
    with TestClient(app) as client:
        resp = client.get(f"{prefix}/kbs", headers=headers)
        assert resp.status_code == 200
        items = resp.json()["items"]
        mine = next(item for item in items if item["kb_id"] == kb_id)
        assert mine["has_index"] is True and mine["name"]
        assert client.get(f"{prefix}/kbs", headers={"X-API-Token": "wrong"}).status_code == 401


def test_delete_document_invalidates_retrieval_immediately(indexed_kb):
    """删除后不依赖重建任务：修订号变化使缓存重载，已删内容立即失效（§5.3）。"""
    kb_id, settings, builder = indexed_kb
    cache = IndexCacheManager(settings)
    service = RetrievalService(cache, MockEmbeddingGateway(dim=1024), settings)

    before = asyncio_run(service.retrieve(kb_id=kb_id, tenant_id=1, question="报销时限"))
    assert before.status == "ok"
    assert any("报销" in e.content for e in before.evidence)

    with SessionLocal() as session:
        document = session.execute(
            select(KbDocument).where(KbDocument.kb_id == kb_id, KbDocument.title == "报销制度")
        ).scalar_one()
        document_id = document.id
    # 通过服务层执行与 API 相同的删除事务
    asyncio_run(delete_via_api_logic(document_id))

    after = asyncio_run(service.retrieve(kb_id=kb_id, tenant_id=1, question="报销时限"))
    assert all("报销" not in e.content for e in after.evidence)
    # 仅剩考勤文档：报销查询无词项命中 → 保守拒答或空证据
    assert after.status in ("ok", "insufficient_evidence")
    kb = get_kb(kb_id)
    assert kb.data_revision >= 2
    jobs = session_jobs(kb_id)
    assert {j.job_type for j in jobs} >= {"reindex", "cleanup"}


def test_delete_guards(indexed_kb):
    kb_id, settings, builder = indexed_kb
    with SessionLocal() as session:
        document = session.execute(
            select(KbDocument).where(KbDocument.kb_id == kb_id)
        ).scalars().first()
        document_id = document.id
    with SessionLocal() as session:
        session.get(KbDocument, document_id).deleted_at = dt.datetime.now(dt.UTC)
        session.commit()

    # 重复删除 → 409
    with pytest.raises(Conflict, match="已删除"):
        delete_document_direct(document_id)


def test_equal_content_different_keys_and_same_key_replacement(indexed_kb, fake_minio, monkeypatch):
    from datetime import date

    from app.storage import build_object_key

    day = date.today()
    monkeypatch.setattr(
        "app.api.documents.build_object_key",
        lambda user_id, filename: build_object_key(user_id, filename, local_date=day),
    )
    kb_id, settings, builder = indexed_kb
    content = "重复内容不再跨文件去重。".encode()
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        with TestClient(app) as client:
            headers = {"X-API-Token": get_settings().api_token}
            prefix = get_settings().api_prefix
            first = client.post(
                f"{prefix}/kbs/{kb_id}/documents",
                files={"file": ("新文件.txt", content, "text/plain")}, headers=headers,
            )
            second = client.post(
                f"{prefix}/kbs/{kb_id}/documents",
                files={"file": ("换个名字.txt", content, "text/plain")}, headers=headers,
            )
            assert first.status_code == 202, first.text
            assert second.status_code == 202, second.text
            assert second.json()["document_id"] != first.json()["document_id"]
            assert second.json()["job_id"] != first.json()["job_id"]

            # 同名、相同内容由其他用户上传时，用户分段不同，不得跨用户去重或覆盖。
            from app.api.deps import _current_principal
            from app.core.auth import Principal

            app.dependency_overrides[_current_principal] = lambda: Principal(
                tenant_id=1, user_id=2, username="other", is_admin=True
            )
            try:
                other = client.post(
                    f"{prefix}/kbs/{kb_id}/documents",
                    files={"file": ("新文件.txt", content, "text/plain")}, headers=headers,
                )
            finally:
                app.dependency_overrides.pop(_current_principal, None)
            assert other.status_code == 202, other.text
            assert other.json()["document_id"] not in {
                first.json()["document_id"], second.json()["document_id"]
            }
            with SessionLocal() as session:
                other_uri = session.scalar(select(KbDocumentVersion.storage_uri).where(
                    KbDocumentVersion.document_id == other.json()["document_id"]
                ))
            assert fake_minio.contains(other_uri)

            from app.storage import parse_object_uri

            with SessionLocal() as session:
                first_uri = session.scalar(select(KbDocumentVersion.storage_uri).where(
                    KbDocumentVersion.document_id == first.json()["document_id"]
                ))
            key = parse_object_uri(first_uri, bucket=settings.minio_bucket)
            other_key = parse_object_uri(other_uri, bucket=settings.minio_bucket)
            assert other_key != key
            before = len(fake_minio.events)
            replacement = client.post(
                f"{prefix}/kbs/{kb_id}/documents",
                files={"file": ("新文件.txt", b"replacement content", "text/plain")},
                headers=headers,
            )
            assert replacement.status_code == 202, replacement.text
            assert replacement.json()["document_id"] != first.json()["document_id"]
            assert fake_minio.events[before:] == [("delete", key), ("put", key)]
            assert fake_minio.objects[(settings.minio_bucket, key)] == b"replacement content"
            assert fake_minio.objects[(settings.minio_bucket, other_key)] == content
            with SessionLocal() as session:
                assert session.get(KbDocument, first.json()["document_id"]).deleted_at is not None
                assert session.get(KbDocument, second.json()["document_id"]).deleted_at is None
                assert session.get(KbDocument, other.json()["document_id"]).deleted_at is None
    finally:
        app.dependency_overrides.pop(get_settings, None)


def test_cleanup_purges_expired_protocol_versions_and_objects(indexed_kb, fake_minio, monkeypatch):
    kb_id, settings, builder = indexed_kb
    # 真实时序：上传额外文档（快照2 含它）→ 软删除 → 重建（快照3 不含它）
    # → 快照1/2 变为非活跃且过期 → 旧快照清除后其屏障内版本可安全物理清除。
    dvid = upload_extra(kb_id, settings, "额外.txt", "额外的内容条款。".encode())
    asyncio.run(builder.build(kb_id=kb_id, new_doc_version_id=dvid))
    with SessionLocal() as session:
        document = session.execute(
            select(KbDocument).where(KbDocument.kb_id == kb_id, KbDocument.title == "额外")
        ).scalar_one()
        document_id = document.id
        purged_uri = session.execute(
            select(KbDocumentVersion.storage_uri).where(
                KbDocumentVersion.document_id == document_id,
                KbDocumentVersion.version_no == document.current_version_no,
            )
        ).scalar_one()
        session.execute(
            KbDocument.__table__.update()
            .where(KbDocument.id == document_id)
            .values(deleted_at=dt.datetime.now(dt.UTC))
        )
        session.commit()
    asyncio.run(builder.build(kb_id=kb_id, new_doc_version_id=None))  # reindex 语义

    with SessionLocal() as session:
        active_id = session.get(KbCollection, kb_id).active_index_id
        stale_ids = [
            row
            for row in session.execute(
                select(KbIndex.id).where(KbIndex.kb_id == kb_id)
            ).scalars()
            if row != active_id
        ]
        assert stale_ids
        session.execute(
            KbIndex.__table__.update()
            .where(KbIndex.id.in_(stale_ids))
            .values(created_at=dt.datetime.now(dt.UTC) - dt.timedelta(hours=48))
        )
        session.execute(
            KbDocument.__table__.update()
            .where(KbDocument.id == document_id)
            .values(deleted_at=dt.datetime.now(dt.UTC) - dt.timedelta(hours=48))
        )
        session.commit()

    # cleanup 构造真实客户端会触网；注入 fake，与上传测试同样的协议保证。
    monkeypatch.setattr(cleanup, "MinioStorage", fake_minio.storage)
    counts = cleanup_kb_scope(
        SessionLocal,
        settings.model_copy(update={"cleanup_retention_hours": 24}),
        kb_id=kb_id,
    )
    assert counts["indexes_removed"] == len(stale_ids)
    assert counts["documents_purged"] == 1
    assert counts["documents_retained"] == 0 and counts["versions_retained"] == 0
    assert counts["objects_deleted"] == 1

    with SessionLocal() as session:
        remaining_indexes = session.execute(
            select(func.count()).select_from(KbIndex).where(KbIndex.kb_id == kb_id)
        ).scalar_one()
        assert remaining_indexes == 1  # 活跃快照保留
        assert session.scalar(
            select(func.count())
            .select_from(KbDocumentVersion)
            .where(KbDocumentVersion.document_id == document_id)
        ) == 0
        assert session.get(KbDocument, document_id) is None
        guard_row = session.execute(
            select(upload_object_guard).where(
                upload_object_guard.c.uri_hash
                == hashlib.sha256(purged_uri.encode("utf-8")).hexdigest()
            )
        ).mappings().one()
    assert guard_row["state"] == "ready" and guard_row["token"] is None
    assert not fake_minio.contains(purged_uri)  # 屏障内、无引用 → 实际删除


def test_cleanup_retains_legacy_versions_without_guard(indexed_kb, fake_minio):
    """迁移前遗留对象（无屏障行）：只保守保留并计数，绝不删除、不抛错。"""
    kb_id, settings, builder = indexed_kb
    uri, digest = fake_minio.seed(settings, "遗留.txt", "迁移前遗留文件，无上传协议屏障。".encode())
    with SessionLocal() as session:
        document = KbDocument(
            kb_id=kb_id,
            title="遗留",
            deleted_at=dt.datetime.now(dt.UTC) - dt.timedelta(hours=48),
        )
        session.add(document)
        session.flush()
        session.add(KbDocumentVersion(
            kb_id=kb_id, document_id=document.id, version_no=1, title="遗留",
            file_sha256=digest, storage_uri=uri, parse_status="succeeded",
        ))
        kb = session.get(KbCollection, kb_id)
        kb.data_revision += 1
        session.commit()

    counts = cleanup_kb_scope(
        SessionLocal,
        settings.model_copy(update={"cleanup_retention_hours": 24}),
        kb_id=kb_id,
    )
    assert counts["versions_retained"] == 1 and counts["objects_deleted"] == 0
    assert counts["documents_purged"] == 0 and counts["documents_retained"] == 1
    assert fake_minio.contains(uri)
    with SessionLocal() as session:
        assert session.scalar(
            select(func.count()).select_from(KbDocumentVersion)
            .where(KbDocumentVersion.kb_id == kb_id)
        ) == 3
        assert session.scalar(select(func.count()).select_from(upload_object_guard)) == 2



def test_delete_kb_full_lifecycle(indexed_kb, fake_minio, monkeypatch):
    kb_id, settings, builder = indexed_kb
    headers = {"X-API-Token": get_settings().api_token}
    prefix = get_settings().api_prefix
    with TestClient(app) as client:
        resp = client.delete(f"{prefix}/kbs/{kb_id}", headers=headers)
        assert resp.status_code == 200, resp.text
        # 列表不再出现
        kb_list = client.get(f"{prefix}/kbs", headers=headers).json()["items"]
        assert all(item["kb_id"] != kb_id for item in kb_list)
        # 知识库级接口全部 404
        assert client.get(f"{prefix}/kbs/{kb_id}/documents", headers=headers).status_code == 404
        stream_resp = client.post(
            f"{prefix}/qa/stream",
            json={"kb_id": kb_id, "question": "报销"},
            headers=headers,
        )
        assert stream_resp.status_code == 404
        # 二次删除 404（已删知识库按策略隐藏）
        assert client.delete(f"{prefix}/kbs/{kb_id}", headers=headers).status_code == 404

    # 回拨保留期：夹具对象均已登记 ready 屏障且无存活引用 → 整库内容
    # 原子预检通过，快照、块、版本与对象全部物理清除；知识库父行作为
    # tombstone 与其任务记录按设计保留（避免删除正在执行的 cleanup job）。
    objects_before = dict(fake_minio.objects)
    with SessionLocal() as session:
        chunks_before = session.scalar(
            select(func.count()).select_from(KbChunk).where(KbChunk.kb_id == kb_id)
        )
        assert chunks_before > 0
        kb = session.get(KbCollection, kb_id)
        kb.deleted_at = dt.datetime.now(dt.UTC) - dt.timedelta(hours=48)
        session.commit()
    monkeypatch.setattr(cleanup, "MinioStorage", fake_minio.storage)
    counts = cleanup_kb_scope(
        SessionLocal,
        settings.model_copy(update={"cleanup_retention_hours": 24}),
        kb_id=kb_id,
    )
    assert counts["kb_retained"] == 1
    assert "kb_purged" not in counts
    assert counts["objects_deleted"] == 2 and counts["versions_retained"] == 0
    assert counts["documents_purged"] == 2
    assert counts["chunks_removed"] == chunks_before
    with SessionLocal() as session:
        kb_row = session.get(KbCollection, kb_id)
        assert kb_row.deleted_at is not None and kb_row.active_index_id is None
        assert session.scalar(
            select(func.count()).select_from(KbChunk).where(KbChunk.kb_id == kb_id)
        ) == 0
        assert session.execute(
            select(func.count()).select_from(KbDocumentVersion)
            .where(KbDocumentVersion.kb_id == kb_id)
        ).scalar_one() == 0
    assert objects_before and not fake_minio.objects  # 屏障内对象已实际删除

def test_cleanup_job_via_worker(indexed_kb):
    kb_id, settings, builder = indexed_kb
    with SessionLocal() as session:
        job = IngestJob(kb_id=kb_id, job_type="cleanup", payload_json={})
        session.add(job)
        session.commit()
        job_id = job.id
    with SessionLocal() as session:
        claimed = claim_next_job(session, "mgmt-worker", lease_seconds=300)
    assert claimed.id == job_id
    asyncio_run(
        execute_claimed(
            claimed,
            builder=builder,
            session_factory=SessionLocal,
            worker_id="mgmt-worker",
            lease_seconds=300,
            settings=settings,
        )
    )
    with SessionLocal() as session:
        assert session.get(IngestJob, job_id).status == "succeeded"


# ---------------------------------------------------------------- helpers


async def delete_via_api_logic(document_id: int) -> None:
    """与 DELETE /documents/{id} 相同的事务语义（绕过 HTTP 层复用断言）。"""
    from app.api.kbs import delete_document
    from app.core.auth import get_admin_principal

    with SessionLocal() as session:
        principal = get_admin_principal()
        delete_document(document_id, session, principal)


def delete_document_direct(document_id: int) -> None:
    from app.api.kbs import delete_document
    from app.core.auth import get_admin_principal

    with SessionLocal() as session:
        principal = get_admin_principal()
        delete_document(document_id, session, principal)


def upload_extra(kb_id: int, settings: Settings, name: str, content: bytes) -> int:
    import hashlib

    from app.parsers.intake import MinioStorage
    from app.storage import build_object_key

    uri = MinioStorage(settings).put(build_object_key(1, name), content)
    digest = hashlib.sha256(content).digest()
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
        seed_object_guard(session, uri)
        kb = session.get(KbCollection, kb_id)
        kb.data_revision += 1
        session.commit()
        return version.id


def get_kb(kb_id: int) -> KbCollection:
    with SessionLocal() as session:
        kb = session.get(KbCollection, kb_id)
        session.expunge(kb)
        return kb


def session_jobs(kb_id: int) -> list[IngestJob]:
    with SessionLocal() as session:
        jobs = session.execute(
            select(IngestJob).where(IngestJob.kb_id == kb_id)
        ).scalars().all()
        for job in jobs:
            session.expunge(job)
        return jobs


def asyncio_run(coro):
    return asyncio.run(coro)
