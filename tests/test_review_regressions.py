"""审查发现问题的离线回归，不连接开发数据库、对象存储或模型。"""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import jwt
import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pgvector.sqlalchemy import Vector
from sqlalchemy import BigInteger, create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import admin, auth, documents, kbs, qa
from app.api import eval as eval_api
from app.api.deps import _current_principal
from app.core import auth as auth_core
from app.core.auth import Principal, hash_password, issue_jwt
from app.core.config import Settings, get_settings
from app.core.errors import Conflict, ServiceUnavailable, register_exception_handlers
from app.db import session as db_session
from app.db.base import Base
from app.db.models import (
    AppUser,
    Conversation,
    IngestJob,
    KbChunk,
    KbCollection,
    KbDocument,
    KbDocumentVersion,
    KbEmbedding,
    KbIndex,
    KbMember,
    OrgTag,
    QaCitation,
    QaRequest,
    UserOrgTag,
)
from app.db.spaces import get_or_create_main_space
from app.gateways.mock import MockEmbeddingGateway
from app.generation.service import QaService
from app.indexing.cache import IndexCacheManager, LoadedIndex
from app.indexing.snapshot import IndexBuilder, RevisionChanged
from app.storage import build_object_key, object_uri
from app.workers.runner import execute_claimed


@compiles(BigInteger, "sqlite")
def _sqlite_bigint(_type, _compiler, **_kwargs):
    return "INTEGER"


@compiles(JSONB, "sqlite")
def _sqlite_json(_type, _compiler, **_kwargs):
    return "JSON"


@compiles(Vector, "sqlite")
def _sqlite_vector(_type, _compiler, **_kwargs):
    return "TEXT"


@pytest.fixture
def sessions(monkeypatch):
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False, autoflush=False)
    monkeypatch.setattr(db_session, "SessionLocal", factory)
    monkeypatch.setattr(auth, "SessionLocal", factory)
    yield factory
    engine.dispose()


@pytest.fixture
def settings(monkeypatch):
    config = Settings(
        _env_file=None, api_token="test-static-admin-token",
        jwt_secret="test-jwt-secret-for-offline-regression-tests",
    )
    monkeypatch.setattr(auth_core, "get_settings", lambda: config)
    monkeypatch.setattr(auth, "get_settings", lambda: config)
    return config


@pytest.fixture
def principal():
    return Principal(tenant_id=1, user_id=2, username="tester")


@pytest.fixture
def client(sessions, settings, principal):
    app = FastAPI()
    register_exception_handlers(app)
    for router in (auth.router, documents.router, kbs.router, qa.router, admin.router,
                   eval_api.router):
        app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[_current_principal] = lambda: principal

    def session_dependency():
        with sessions() as session:
            yield session

    app.dependency_overrides[db_session.get_session] = session_dependency
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app) as test_client:
        yield test_client


def _add_document(session, document_id, kb_id, **attributes):
    session.add(KbDocument(
        id=document_id, kb_id=kb_id, title=f"文档{document_id}",
        uploader_user_id=2, **attributes,
    ))
    session.add(KbDocumentVersion(
        id=document_id, kb_id=kb_id, document_id=document_id, version_no=1,
        title=f"文档{document_id}", file_sha256=b"x" * 32,
        storage_uri=f"s3://nlprag/document{document_id}.txt", parse_status="succeeded",
    ))


def _add_request(session, request_id=1, *, user_id=2, tenant_id=1, kb_id=1):
    session.add(QaRequest(
        id=request_id, request_id=f"qa_{request_id}", tenant_id=tenant_id,
        user_id=user_id, kb_id=kb_id, question="问题", answer_status="answered",
        model_config_json={},
    ))


def _index(index_id=1, **attributes):
    return KbIndex(
        id=index_id, kb_id=1, source_revision=1, status="building",
        embedding_model="test", embedding_dim=1024, pipeline_version="test",
        config_json={}, manifest_json={}, **attributes,
    )


def test_admin_jwt_me_keeps_display_name_and_org_tags(client, sessions):
    with sessions() as session:
        session.add(AppUser(
            id=2, username="tester", display_name="真实姓名", is_admin=True,
            password_hash=hash_password("password"),
        ))
        session.add(OrgTag(id=1, name="研发"))
        session.add(UserOrgTag(user_id=2, org_tag_id=1))
        session.commit()
    response = client.post("/api/v1/auth/login", json={
        "username": "tester", "password": "password",
    })
    token = response.json()["access_token"]
    profile = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert profile.status_code == 200
    assert profile.json()["display_name"] == "真实姓名"
    assert profile.json()["org_tags"] == ["研发"]


@pytest.mark.parametrize("header", ["X-API-Token", "Authorization"])
def test_static_admin_tokens_are_rejected(client, settings, header):
    value = f"Bearer {settings.api_token}" if header == "Authorization" else settings.api_token
    response = client.get("/api/v1/auth/me", headers={header: value})
    assert response.status_code == 401


def test_registration_cannot_self_assign_organization_access(client, sessions):
    with sessions() as session:
        session.add(OrgTag(id=1, name="研发"))
        session.commit()
    payload = {"username": "newuser", "password": "password", "display_name": "新用户"}
    blocked = client.post("/api/v1/auth/register", json={**payload, "org_tags": ["研发"]})
    assert blocked.status_code == 403
    with sessions() as session:
        assert session.scalars(select(AppUser)).all() == []
        assert session.scalars(select(UserOrgTag)).all() == []
    response = client.post("/api/v1/auth/register", json=payload)
    assert response.status_code == 201
    assert response.json()["user"]["org_tags"] == []


def test_registration_rolls_back_if_jwt_is_not_configured(client, sessions, settings):
    settings.jwt_secret = ""
    settings.api_token = ""
    response = client.post("/api/v1/auth/register", json={
        "username": "newuser", "password": "password", "display_name": "新用户",
    })
    assert response.status_code == 503
    with sessions() as session:
        assert session.scalars(select(AppUser)).all() == []


@pytest.mark.parametrize("subject", ["abc", "0", "-1", str(2**63), None])
def test_invalid_jwt_subject_is_unauthorized(client, settings, subject):
    token = jwt.encode(
        {"sub": subject, "exp": datetime.now(UTC) + timedelta(minutes=5)},
        settings.jwt_secret, algorithm="HS256",
    )
    response = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


def test_default_jwt_key_is_replaced_by_stable_separate_key(client, settings):
    config = Settings(_env_file=None, api_token=settings.api_token)
    token = issue_jwt(2, "tester", config)
    key = auth_core._jwt_signing_key(config)
    assert key != settings.api_token
    assert jwt.decode(token, key, algorithms=["HS256"])["sub"] == "2"
    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(token, "dev-jwt-secret-change-me", algorithms=["HS256"])
    for api_token in ("", "change-me"):
        with pytest.raises(ServiceUnavailable):
            issue_jwt(2, "tester", Settings(_env_file=None, api_token=api_token))


def test_document_list_excludes_deleted_kbs_and_other_tenants(client, sessions):
    with sessions() as session:
        session.add_all([
            KbCollection(id=1, tenant_id=1, name="live"),
            KbCollection(id=2, tenant_id=1, name="deleted", deleted_at=datetime.now(UTC)),
            KbCollection(id=3, tenant_id=2, name="other"),
        ])
        for document_id in (1, 2, 3):
            _add_document(session, document_id, document_id, visibility="public")
        session.commit()
    response = client.get("/api/v1/documents")
    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert [row["document_id"] for row in response.json()["items"]] == [1]


def test_main_space_creation_matches_model_constraints(sessions, settings, monkeypatch):
    monkeypatch.setattr("app.db.spaces.get_settings", lambda: settings)
    with sessions() as session:
        first = get_or_create_main_space(session).id
    with sessions() as session:
        assert get_or_create_main_space(session).id == first
        assert len(session.scalars(select(KbCollection)).all()) == 1


def test_deleted_document_jobs_remain_readable_by_the_uploader(client, sessions):
    from app.api.jobs import router as jobs_router

    client.app.include_router(jobs_router, prefix="/api/v1")
    with sessions() as session:
        session.add(KbCollection(id=1, tenant_id=1, name="main"))
        _add_document(session, 1, 1, visibility="private")
        session.commit()
    response = client.delete("/api/v1/documents/1")
    assert response.status_code == 200
    for field in ("reindex_job_id", "cleanup_job_id"):
        assert client.get(f"/api/v1/jobs/{response.json()[field]}").status_code == 200


@pytest.mark.parametrize("deleted,tenant_id", [(False, 1), (True, 1), (False, 2)])
def test_citation_checks_live_kb_and_tenant(client, sessions, deleted, tenant_id):
    with sessions() as session:
        session.add(KbCollection(
            id=1, tenant_id=tenant_id, name="main",
            deleted_at=datetime.now(UTC) if deleted else None,
        ))
        _add_document(session, 1, 1, visibility="public")
        _add_request(session, tenant_id=tenant_id)
        session.add(QaCitation(
            qa_request_id=1, source_id="S1", citation_id="citation", chunk_id=1,
            doc_version_id=1, document_title="文档", locator={}, quote="原文",
        ))
        session.commit()
    response = client.get("/api/v1/citations/citation")
    assert response.status_code == (200 if not deleted and tenant_id == 1 else 404)


@pytest.mark.parametrize("owner,tenant_id,expected", [(2, 1, 201), (3, 1, 404), (2, 2, 404)])
def test_feedback_requires_own_request(client, sessions, owner, tenant_id, expected):
    with sessions() as session:
        _add_request(session, user_id=owner, tenant_id=tenant_id)
        session.commit()
    response = client.post("/api/v1/feedback", json={
        "request_id": "qa_1", "rating": "helpful",
    })
    assert response.status_code == expected


@pytest.mark.parametrize("acl_revision,expected", [(0, "ready"), (1, "superseded")])
def test_snapshot_publish_rechecks_permissions(sessions, settings, acl_revision, expected):
    with sessions() as session:
        session.add(KbCollection(
            id=1, tenant_id=1, name="main", data_revision=1, acl_revision=acl_revision,
        ))
        session.add(_index())
        session.commit()
    builder = IndexBuilder(sessions, MockEmbeddingGateway(), settings)
    if acl_revision:
        with pytest.raises(RevisionChanged):
            builder._publish(1, 1, 1, 0)
    else:
        builder._publish(1, 1, 1, 0)
    with sessions() as session:
        assert session.get(KbIndex, 1).status == expected
        assert session.get(KbCollection, 1).active_index_id == (None if acl_revision else 1)


def test_snapshot_creation_rejects_stale_permissions(sessions, settings):
    with sessions() as session:
        session.add(KbCollection(
            id=1, tenant_id=1, name="main", data_revision=1, acl_revision=1,
        ))
        session.commit()
    builder = IndexBuilder(sessions, MockEmbeddingGateway(), settings)
    with pytest.raises(RevisionChanged):
        builder._create_index_row(1, 1, [], 1, 0)
    with sessions() as session:
        assert session.scalars(select(KbIndex)).all() == []


def test_snapshot_manifest_only_includes_current_document_versions(sessions, settings, monkeypatch):
    with sessions() as session:
        session.add(KbCollection(id=1, tenant_id=1, name="main"))
        _add_document(session, 1, 1, visibility="private")
        session.flush()
        session.get(KbDocument, 1).current_version_no = 2
        session.add(KbDocumentVersion(
            id=2, kb_id=1, document_id=1, version_no=2, title="新版文档",
            file_sha256=b"y" * 32,
            storage_uri=object_uri(settings.minio_bucket, build_object_key(2, "current.txt")),
            parse_status="succeeded",
        ))
        session.commit()
    builder = IndexBuilder(sessions, MockEmbeddingGateway(), settings)
    monkeypatch.setattr(builder, "_load_verified", lambda *args: b"current content")
    monkeypatch.setattr(builder, "_validate", lambda *args: None)
    monkeypatch.setattr(
        "app.indexing.snapshot.run_pipeline", lambda *args: SimpleNamespace(chunks=[])
    )
    report = asyncio.run(builder.build(kb_id=1))
    with sessions() as session:
        assert session.get(KbIndex, report.index_id).manifest_json["files"] == [2]


@pytest.mark.parametrize("status", ["insufficient_evidence", "ok"])
def test_stream_rechecks_revision_before_finishing(sessions, settings, principal, status):
    with sessions() as session:
        session.add(KbCollection(id=1, tenant_id=1, name="main"))
        session.commit()

    def change_revision():
        with sessions() as session:
            session.get(KbCollection, 1).acl_revision += 1
            session.commit()

    async def retrieve(**kwargs):
        if status == "insufficient_evidence":
            change_revision()
        return SimpleNamespace(status=status, mode="hybrid", index_id=None, evidence=[])

    async def generate_stream(**kwargs):
        change_revision()
        yield {"delta": "候选回答 [S1]", "finish_reason": None}

    service = QaService(
        retrieval=SimpleNamespace(retrieve=retrieve),
        chat=SimpleNamespace(generate_stream=generate_stream),
        settings=settings, session_factory=sessions,
    )

    async def collect():
        return [event async for event in service.stream_ask(
            kb_id=1, principal=principal, question="问题",
        )]

    with pytest.raises(Conflict):
        asyncio.run(collect())
    with sessions() as session:
        assert session.scalar(select(QaRequest)).answer_status == "stale_index"


@pytest.mark.parametrize("attempt,expected", [(1, "queued"), (3, "failed")])
def test_revision_changed_job_retries_with_attempt_limit(sessions, settings, attempt, expected):
    with sessions() as session:
        session.add(IngestJob(
            id=1, kb_id=1, job_type="reindex", status="running", attempt=attempt,
            max_attempt=3, lease_owner="worker", payload_json={},
        ))
        session.commit()
        job = session.get(IngestJob, 1)
    builder = SimpleNamespace(build=AsyncMock(side_effect=RevisionChanged("changed")))
    asyncio.run(execute_claimed(
        job, builder=builder, session_factory=sessions, worker_id="worker",
        lease_seconds=300, settings=settings,
    ))
    with sessions() as session:
        assert session.get(IngestJob, 1).status == expected


def test_cache_uses_current_document_permissions(sessions, settings):
    with sessions() as session:
        session.add(KbCollection(id=1, tenant_id=1, name="main", active_index_id=1))
        _add_document(session, 1, 1, visibility="private")
        index = _index()
        index.status = "ready"
        session.add(index)
        session.add(KbChunk(
            id=1, kb_id=1, index_id=1, document_id=1, doc_version_id=1, chunk_no=0,
            title_path="", content="原文", search_text="原文", embedding_input="原文",
            input_sha256=b"x" * 32, tokens=[], keywords=[], source_locator={},
            visibility="public", uploader_user_id=999,
        ))
        session.add(KbEmbedding(chunk_id=1, embedding=np.zeros(1024)))
        session.commit()
    loaded = IndexCacheManager(settings)._load(sessions, 1, 1, 1)
    assert loaded.records[1].visibility == "private"
    assert loaded.records[1].uploader_user_id == 2


def test_cache_does_not_reuse_another_tenants_entry(sessions, settings):
    cache = IndexCacheManager(settings)
    cache.swap(LoadedIndex(kb_id=1, index_id=1, tenant_id=2, data_revision=0, acl_revision=0))
    with pytest.raises(Conflict):
        asyncio.run(cache.ensure_loaded(
            session_factory=sessions, kb_id=1, tenant_id=1, expected_index_id=1,
            expected_data_revision=0, expected_acl_revision=0,
        ))


def test_last_admin_cannot_be_demoted(client, sessions, principal):
    principal.is_admin = True
    with sessions() as session:
        session.add(AppUser(
            id=2, username="tester", display_name="管理员", password_hash="unused", is_admin=True,
        ))
        session.commit()
    assert client.patch("/api/v1/admin/users/2", json={"is_admin": False}).status_code == 409
    with sessions() as session:
        session.add(AppUser(
            id=3, username="other", display_name="其他管理员",
            password_hash="unused", is_admin=True,
        ))
        session.commit()
    assert client.patch("/api/v1/admin/users/2", json={"is_admin": False}).status_code == 200


@pytest.mark.parametrize("path", ["/documents", "/kbs/1/documents"])
def test_oversized_upload_is_rejected(client, sessions, principal, settings, path):
    principal.is_admin = True
    settings.max_upload_bytes = 4
    with sessions() as session:
        session.add(KbCollection(id=1, tenant_id=1, name="main"))
        session.commit()
    response = client.post("/api/v1" + path, files={"file": ("large.txt", b"x" * 100)})
    assert response.status_code == 413
    with sessions() as session:
        assert session.scalars(select(KbDocument)).all() == []


@pytest.mark.parametrize("query,expected,total", [
    ([], [1, 2, 3, 4, 5], 5),
    ([("org_tag_ids", 1), ("org_tag_ids", 2)], [1, 2, 3], 3),
    ([("org_tag_ids", 2), ("org_tag_ids", 2)], [1, 2], 2),
    ([("org_tag_ids", 999)], [], 0),
    ([("keyword", "ALIce")], [1], 1),
    ([("keyword", "  研发  ")], [2], 1),
    ([("keyword", "_")], [4], 1),
    ([("keyword", "%")], [], 0),
    ([("is_admin", "true")], [1], 1),
    ([("is_admin", "false")], [2, 3, 4, 5], 4),
    ([("org_tag_ids", 1), ("org_tag_ids", 2), ("is_admin", "false")], [2, 3], 2),
    ([("org_tag_ids", 1), ("is_admin", "false"), ("keyword", "bob")], [], 0),
    ([("org_tag_ids", 1), ("org_tag_ids", 2), ("limit", 1), ("offset", 1)], [2], 3),
    ([("org_tag_ids", 1), ("limit", 1), ("offset", 99)], [], 2),
])
def test_user_search_filters_and_pagination(client, sessions, principal, query, expected, total):
    principal.is_admin = True
    with sessions() as session:
        for user_id, username, name in [
            (1, "alice", "Alice"), (2, "bob", "研发成员"), (3, "carol", "Carol"),
            (4, "with_under", "无标签"), (5, "withXunder", "对照"),
        ]:
            session.add(AppUser(
                id=user_id, username=username, display_name=name,
                password_hash="unused", is_admin=user_id == 1,
            ))
        session.add_all([OrgTag(id=1, name="研发"), OrgTag(id=2, name="产品")])
        for user_id, tag_id in [(1, 1), (1, 2), (2, 2), (3, 1)]:
            session.add(UserOrgTag(user_id=user_id, org_tag_id=tag_id))
        session.commit()
    response = client.get("/api/v1/admin/users", params=query)
    assert response.status_code == 200
    assert response.json()["total"] == total
    assert [user["user_id"] for user in response.json()["items"]] == expected


def test_user_deletion_cleans_memberships_and_invalidates_login(
    client, sessions, principal, settings
):
    principal.is_admin = True
    with sessions() as session:
        session.add_all([
            AppUser(id=2, username="admin", display_name="管理员", password_hash="unused",
                    is_admin=True),
            AppUser(id=3, username="removed", display_name="待删除用户",
                    password_hash=hash_password("password"), is_admin=False),
            OrgTag(id=1, name="研发"),
            KbCollection(id=1, tenant_id=1, name="main"),
            UserOrgTag(user_id=3, org_tag_id=1),
            KbMember(kb_id=1, user_id=3, role="viewer"),
            Conversation(id=1, tenant_id=1, user_id=3, title="历史会话"),
        ])
        _add_document(session, 1, 1, visibility="private")
        _add_request(session, user_id=3)
        session.flush()
        session.get(KbDocument, 1).uploader_user_id = 3
        session.commit()
    token = issue_jwt(3, "removed", settings)
    with sessions() as session:
        session.connection().exec_driver_sql("PRAGMA foreign_keys=ON")
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/v1/auth/me", headers=headers).status_code == 200
    response = client.delete("/api/v1/admin/users/3")
    assert response.status_code == 204, response.json()
    assert client.get("/api/v1/auth/me", headers=headers).status_code == 401
    assert client.post("/api/v1/auth/login", json={
        "username": "removed", "password": "password",
    }).status_code == 401
    with sessions() as session:
        assert session.get(AppUser, 3) is None
        assert session.get(UserOrgTag, (3, 1)) is None
        assert session.get(KbMember, (1, 3)) is None
        assert session.get(Conversation, 1) is None
        assert session.get(KbDocument, 1) is not None
        assert session.get(QaRequest, 1) is not None
        assert session.get(OrgTag, 1) is not None


def test_user_delete_protects_current_account_and_last_admin(client, sessions, principal):
    principal.is_admin = True
    with sessions() as session:
        session.add(AppUser(
            id=2, username="admin", display_name="管理员", password_hash="unused", is_admin=True,
        ))
        session.commit()
    response = client.delete("/api/v1/admin/users/2")
    assert response.status_code == 409
    assert response.json()["message"] == "至少保留一名管理员"
    with sessions() as session:
        session.add(AppUser(
            id=3, username="another", display_name="其他管理员",
            password_hash="unused", is_admin=True,
        ))
        session.commit()
    response = client.delete("/api/v1/admin/users/2")
    assert response.status_code == 409
    assert response.json()["message"] == "不能删除当前登录账号"
    assert client.delete("/api/v1/admin/users/3").status_code == 204
    assert client.delete("/api/v1/admin/users/3").status_code == 404
    with sessions() as session:
        assert session.get(AppUser, 2).is_admin


@pytest.mark.parametrize("method,path", [
    ("GET", "/admin/users"), ("DELETE", "/admin/users/3"),
])
def test_user_management_requires_admin(client, method, path):
    assert client.request(method, "/api/v1" + path).status_code == 403


def test_eval_endpoints_require_admin_and_aggregate_files(client, principal):
    # 非管理员：聚合与明细接口均 403（Principal.is_admin 默认 False）
    assert client.get("/api/v1/eval/overview").status_code == 403
    assert client.get("/api/v1/eval/details/qa").status_code == 403

    principal.is_admin = True
    body = client.get("/api/v1/eval/overview").json()
    assert set(body) >= {"retrieval", "qa", "mcq", "preference", "system_perf", "meta"}
    # 结果目录存在时 qa/mcq 汇总可用；不存在也只会是 null，不报错
    assert "error" not in body

    assert client.get("/api/v1/eval/details/nope").status_code == 404
    details = client.get("/api/v1/eval/details/mcq")
    assert details.status_code == 200
    assert details.json()["track"] == "mcq"
