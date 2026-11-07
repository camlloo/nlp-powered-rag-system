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
def client(pg_ready, tmp_path):
    test_settings = Settings(storage_dir=str(tmp_path))
    app.dependency_overrides[get_settings] = lambda: test_settings
    with TestClient(app) as test_client:
        yield test_client, str(tmp_path)
    app.dependency_overrides.pop(get_settings, None)


def auth_headers():
    from app.core.config import get_settings as gs

    return {"X-API-Token": gs().api_token}


def run_pending_job(storage_dir: str) -> tuple[int, str]:
    """模拟 worker：领取一个任务并执行完成，返回 (job_id, status)。"""
    settings = Settings(storage_dir=storage_dir)
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
    test_client, tmp_path = client
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
        _, status = run_pending_job(tmp_path)
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


def test_idempotency_replay_and_conflict(client):
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
        second = test_client.post(f"{prefix}/kbs/{kb_id}/documents", **kwargs)
        assert second.status_code == 202
        assert second.headers.get("Idempotent-Replay") == "true"
        assert second.json()["job_id"] == first.json()["job_id"]  # 返回既有结果

        other = test_client.post(
            f"{prefix}/kbs/{kb_id}/documents",
            files={"file": ("其他.txt", "另外一份完全不同的内容".encode(), "text/plain")},
            headers={**auth_headers(), "Idempotency-Key": "key-1"},
        )
        assert other.status_code == 409  # 同键不同内容
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
