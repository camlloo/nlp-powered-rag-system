"""API 端到端集成测试：创建知识库 → 上传 → worker 构建 → 任务状态（M4 验收链路）。"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.db.session import SessionLocal
from app.gateways.mock import MockEmbeddingGateway
from app.indexing.snapshot import IndexBuilder
from app.main import app
from app.workers.runner import claim_next_job, execute_claimed
from tests.integration.helpers import cleanup_kb

FILE_CONTENT = "第三章 费用报销。员工应在出差结束后 30 日内申请。交通费凭有效票据报销。".encode()


@pytest.fixture
def client(pg_ready, test_settings):
    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        with TestClient(app) as test_client:
            yield test_client, test_settings
    finally:
        app.dependency_overrides.pop(get_settings, None)


def auth_headers():
    from app.core.config import get_settings as gs

    return {"X-API-Token": gs().api_token}


def run_pending_job(settings: Settings) -> tuple[int, str]:
    """模拟 worker：领取一个任务并执行完成，返回 (job_id, status)。"""
    builder = IndexBuilder(
        SessionLocal, MockEmbeddingGateway(dim=1024), settings, worker_id="it-worker"
    )
    with SessionLocal() as session:
        job = claim_next_job(session, "it-worker", lease_seconds=300)
    assert job is not None, "没有可领取的任务"
    asyncio.run(
        execute_claimed(
            job,
            builder=builder,
            session_factory=SessionLocal,
            worker_id="it-worker",
            lease_seconds=300,
        )
    )
    from app.db.models import IngestJob

    with SessionLocal() as session:
        return job.id, session.get(IngestJob, job.id).status


def test_full_flow_upload_build_query_status(client):
    test_client, settings = client
    prefix = get_settings().api_prefix

    # 1. 创建知识库
    resp = test_client.post(f"{prefix}/kbs", json={"name": "E2E 知识库"}, headers=auth_headers())
    assert resp.status_code == 201, resp.text
    kb_id = resp.json()["kb_id"]
    try:
        # 2. 上传文档 -> 202 + job_id
        resp = test_client.post(
            f"{prefix}/kbs/{kb_id}/documents",
            files={"file": ("制度.txt", FILE_CONTENT, "text/plain")},
            headers=auth_headers(),
        )
        assert resp.status_code == 202, resp.text
        job_id = resp.json()["job_id"]
        assert resp.json()["version_no"] == 1

        # 3. 任务尚未执行时状态为 queued
        resp = test_client.get(f"{prefix}/jobs/{job_id}", headers=auth_headers())
        assert resp.status_code == 200
        assert resp.json()["status"] in ("queued", "running")

        # 4. 模拟 worker 执行
        _, status = run_pending_job(settings)
        assert status == "succeeded"

        # 5. 任务状态与索引发布
        resp = test_client.get(f"{prefix}/jobs/{job_id}", headers=auth_headers())
        body = resp.json()
        assert body["status"] == "succeeded"
        from app.db.models import KbCollection

        with SessionLocal() as session:
            kb = session.get(KbCollection, kb_id)
            session.expunge(kb)
        assert kb.active_index_id is not None
    finally:
        cleanup_kb(kb_id)


def test_idempotency_replay_and_conflict(client, fake_minio):
    test_client, _ = client
    prefix = get_settings().api_prefix
    kb_id = test_client.post(
        f"{prefix}/kbs", json={"name": "幂等库"}, headers=auth_headers()
    ).json()["kb_id"]
    try:
        kwargs = {
            "files": {"file": ("制度.txt", FILE_CONTENT, "text/plain")},
            "headers": {**auth_headers(), "Idempotency-Key": "key-1"},
        }
        first = test_client.post(f"{prefix}/kbs/{kb_id}/documents", **kwargs)
        assert first.status_code == 202
        from sqlalchemy import select

        from app.db.upload_guard import upload_idempotency, upload_object_guard

        with SessionLocal() as session:
            guard = session.execute(select(upload_object_guard)).mappings().one()
            idem = session.execute(select(upload_idempotency)).mappings().one()
            assert (guard["state"], guard["token"]) == ("ready", None)
            assert (idem["state"], idem["job_id"]) == ("completed", first.json()["job_id"])
        events_after_first = list(fake_minio.events)
        assert [op for op, _ in events_after_first] == ["delete", "put"]
        second = test_client.post(f"{prefix}/kbs/{kb_id}/documents", **kwargs)
        assert fake_minio.events == events_after_first
        assert second.status_code == 202
        assert second.headers.get("Idempotent-Replay") == "true"
        assert second.json()["job_id"] == first.json()["job_id"]  # 返回既有结果

        other = test_client.post(
            f"{prefix}/kbs/{kb_id}/documents",
            files={"file": ("其他.txt", "另外一份完全不同的内容".encode(), "text/plain")},
            headers={**auth_headers(), "Idempotency-Key": "key-1"},
        )
        assert other.status_code == 409  # 同键不同内容
        assert fake_minio.events == events_after_first
    finally:
        cleanup_kb(kb_id)


def test_existing_writing_guard_blocks_upload_without_storage_io(client, fake_minio, monkeypatch):
    import hashlib
    from datetime import date

    from sqlalchemy import insert, select

    from app.db.upload_guard import upload_object_guard
    from app.storage import build_object_key, object_uri

    day = date.today()
    monkeypatch.setattr(
        "app.api.documents.build_object_key",
        lambda user_id, filename: build_object_key(user_id, filename, local_date=day),
    )

    test_client, settings = client
    prefix = get_settings().api_prefix
    kb_id = test_client.post(
        f"{prefix}/kbs", json={"name": "写入屏障"}, headers=auth_headers()
    ).json()["kb_id"]
    try:
        uri = object_uri(settings.minio_bucket, build_object_key(1, "blocked.txt", local_date=day))
        with SessionLocal() as session:
            session.execute(insert(upload_object_guard).values(
                uri_hash=hashlib.sha256(uri.encode()).hexdigest(), uri=uri,
                state="writing", token="x" * 32,
            ))
            session.commit()
        response = test_client.post(
            f"{prefix}/kbs/{kb_id}/documents", headers=auth_headers(),
            files={"file": ("blocked.txt", FILE_CONTENT, "text/plain")},
        )
        assert response.status_code == 409, response.text
        assert fake_minio.events == []
        with SessionLocal() as session:
            guard = session.execute(select(upload_object_guard)).mappings().one()
            assert (guard["state"], guard["token"]) == ("writing", "x" * 32)
    finally:
        cleanup_kb(kb_id)


def test_auth_required(client):
    test_client, _ = client
    prefix = get_settings().api_prefix
    assert test_client.post(f"{prefix}/kbs", json={"name": "x"}).status_code == 401
    assert test_client.get(f"{prefix}/jobs/1").status_code == 401


def test_upload_rejections(client):
    test_client, _ = client
    prefix = get_settings().api_prefix
    kb_id = test_client.post(
        f"{prefix}/kbs", json={"name": "拒绝用例"}, headers=auth_headers()
    ).json()["kb_id"]
    try:
        # 不支持的格式 422
        resp = test_client.post(
            f"{prefix}/kbs/{kb_id}/documents",
            files={"file": ("恶意.exe", b"MZ9090", "application/octet-stream")},
            headers=auth_headers(),
        )
        assert resp.status_code == 422
        # 空文件 413
        resp = test_client.post(
            f"{prefix}/kbs/{kb_id}/documents",
            files={"file": ("空.txt", b"", "text/plain")},
            headers=auth_headers(),
        )
        assert resp.status_code == 413
        # 跨知识库资源 404
        resp = test_client.get(f"{prefix}/jobs/99999999", headers=auth_headers())
        assert resp.status_code == 404
    finally:
        cleanup_kb(kb_id)
