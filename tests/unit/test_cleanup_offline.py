"""离线清理测试：仅内存 SQLite 和 fake storage，不连接 PostgreSQL/MinIO。

SQLite 只能验证顺序、引用、事务及失败处理，不能替代 PostgreSQL 并发行锁测试。
"""

import datetime as dt
import hashlib
import socket
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import (
    IngestJob,
    KbChunk,
    KbCollection,
    KbDocument,
    KbDocumentVersion,
    KbEmbedding,
    KbIndex,
)
from app.db.upload_guard import upload_object_guard
from app.indexing import cleanup
from app.storage import build_object_key, object_uri


@compiles(JSONB, "sqlite")
def _json_for_sqlite(_type, _compiler, **_kw):
    return "JSON"


NOW = dt.datetime(2026, 11, 8, 12)
OLD = NOW - dt.timedelta(hours=48)


class FakeSessions:
    """对内存 DB 的 Session 仿真 PG 前置检查，可注入提交前/后故障。"""

    def __init__(self, factory):
        self.factory = factory
        self.events = []
        self.fail_commit = None

    def __call__(self):
        return FakeSession(self)


class FakeSession:
    def __init__(self, owner):
        self.owner = owner
        self.session = owner.factory()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.session.close()

    def __getattr__(self, name):
        return getattr(self.session, name)

    def get_bind(self):
        return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    def scalar(self, statement, *args, **kwargs):
        if str(statement) == "SHOW transaction_isolation":
            return "read committed"
        return self.session.scalar(statement, *args, **kwargs)

    def commit(self):
        state = self.session.execute(select(upload_object_guard.c.state)).scalars().first()
        phase = "prepare" if state == "writing" else "final"
        failure = self.owner.fail_commit
        if failure == (phase, "before"):
            self.owner.fail_commit = None
            raise RuntimeError("injected commit failure before commit")
        self.session.commit()
        self.owner.events.append(("commit", phase))
        if failure == (phase, "after"):
            self.owner.fail_commit = None
            raise RuntimeError("injected ambiguous commit")


class FakeStorage:
    safe_mutation_no_retry = True

    def __init__(self, sessions):
        self.sessions = sessions
        self.objects = set()
        self.deleted = []
        self.fail = False
        self.on_delete = None

    def delete(self, uri):
        with self.sessions.factory() as session:
            guard = session.execute(select(upload_object_guard)).mappings().one()
            assert guard["state"] == "writing" and guard["token"]
            assert session.scalar(select(KbDocumentVersion.id)) is not None
        self.sessions.events.append(("delete", uri))
        self.deleted.append(uri)
        if self.on_delete:
            self.on_delete()
        self.objects.discard(uri)
        if self.fail:
            raise TimeoutError("simulated uncertain remote outcome")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("离线测试禁止网络连接")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.fixture
def env(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    tables = [
        KbCollection.__table__, KbDocument.__table__, KbDocumentVersion.__table__,
        KbIndex.__table__, KbChunk.__table__, KbEmbedding.__table__, IngestJob.__table__,
    ]
    Base.metadata.create_all(engine, tables=tables)
    upload_object_guard.create(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    sessions = FakeSessions(factory)
    storage = FakeStorage(sessions)
    settings = SimpleNamespace(cleanup_retention_hours=24, minio_bucket="isolated-test-bucket")
    monkeypatch.setattr(cleanup, "MinioStorage", lambda _settings: storage)
    yield SimpleNamespace(
        engine=engine, factory=factory, sessions=sessions, storage=storage, settings=settings
    )
    engine.dispose()


def uri(env, filename="document.txt"):
    key = build_object_key(1, filename, local_date=dt.date(2026, 11, 7))
    return object_uri(env.settings.minio_bucket, key)


def seed(env, *, with_guard=True, deleted=OLD, kb_deleted=None, uri_value=None):
    uri_value = uri_value or uri(env)
    with env.factory.begin() as s:
        s.add(KbCollection(id=1, tenant_id=1, name="test", deleted_at=kb_deleted))
        s.add(KbDocument(id=10, kb_id=1, title="test", deleted_at=deleted))
        s.flush()
        s.add(KbDocumentVersion(
            id=100, kb_id=1, document_id=10, version_no=1, title="test",
            file_sha256=b"x" * 32, storage_uri=uri_value,
        ))
        if with_guard:
            s.execute(upload_object_guard.insert().values(
                uri_hash=hashlib.sha256(uri_value.encode()).hexdigest(),
                uri=uri_value, state="ready", token=None,
            ))
    env.storage.objects.add(uri_value)
    return uri_value


def run(env):
    return cleanup.cleanup_kb_scope(env.sessions, env.settings, kb_id=1, now=NOW)


def guard(env):
    with env.factory() as s:
        return s.execute(select(upload_object_guard)).mappings().one_or_none()


def version(env, version_id=100):
    with env.factory() as s:
        return s.get(KbDocumentVersion, version_id)


def add_index(env, index_id, *, files, active=False, created=OLD, status="ready"):
    with env.factory.begin() as s:
        s.add(KbIndex(
            id=index_id, kb_id=1, source_revision=0, status=status,
            embedding_model="mock", embedding_dim=1024, pipeline_version="test",
            config_json={}, manifest_json={"files": files}, created_at=created,
        ))
        if active:
            s.get(KbCollection, 1).active_index_id = index_id


def test_expired_document_cleans_object_and_nullable_job_fk(env):
    object_uri_value = seed(env)
    with env.factory.begin() as s:
        s.add(IngestJob(
            id=300, kb_id=1, doc_version_id=100, job_type="ingest_file",
            status="succeeded", payload_json={},
        ))
    result = run(env)
    assert result["objects_deleted"] == result["documents_purged"] == 1
    assert env.storage.deleted == [object_uri_value]
    assert env.sessions.events.index(("commit", "prepare")) < env.sessions.events.index(
        ("delete", object_uri_value)
    )
    assert guard(env)["state"] == "ready" and guard(env)["token"] is None
    assert version(env) is None
    with env.factory() as s:
        assert s.get(IngestJob, 300).doc_version_id is None
        assert s.get(KbDocument, 10) is None


def test_shared_uri_keeps_object_and_other_global_reference(env):
    shared = seed(env)
    with env.factory.begin() as s:
        s.add(KbCollection(id=2, tenant_id=2, name="other"))
        s.add(KbDocument(id=20, kb_id=2, title="other"))
        s.flush()
        s.add(KbDocumentVersion(
            id=200, kb_id=2, document_id=20, version_no=1, title="other",
            file_sha256=b"y" * 32, storage_uri=shared,
        ))
    result = run(env)
    assert result["documents_purged"] == 1 and result["objects_deleted"] == 0
    assert not env.storage.deleted and shared in env.storage.objects
    assert version(env) is None and version(env, 200) is not None


def test_expired_snapshot_removed_but_active_manifest_blocks_version(env):
    seed(env)
    add_index(env, 500, files=[100])
    add_index(env, 501, files=[100], active=True)
    with pytest.raises(cleanup.CleanupDeferred, match="快照"):
        run(env)
    with env.factory() as s:
        assert s.get(KbIndex, 500) is None
        assert s.get(KbIndex, 501) is not None
    assert version(env) is not None and not env.storage.deleted
    with env.factory.begin() as s:
        s.get(KbCollection, 1).active_index_id = None
    result = run(env)
    assert result["indexes_removed"] == result["objects_deleted"] == 1


def test_expired_snapshot_removes_embedding_and_chunk_before_version(env):
    seed(env)
    add_index(env, 500, files=[100])
    with env.factory.begin() as s:
        s.add(KbChunk(
            id=600, kb_id=1, index_id=500, document_id=10, doc_version_id=100,
            chunk_no=0, title_path="test", content="test", search_text="test",
            embedding_input="test", input_sha256=b"x" * 32, tokens=[], keywords=[],
            source_locator={},
        ))
        s.flush()
        s.add(KbEmbedding(chunk_id=600, embedding=[0.0] * 1024))
    result = run(env)
    assert result["indexes_removed"] == result["chunks_removed"] == 1
    assert result["objects_deleted"] == 1
    with env.factory() as s:
        assert s.get(KbEmbedding, 600) is None
        assert s.get(KbChunk, 600) is None
        assert s.get(KbIndex, 500) is None


def test_malformed_or_building_snapshot_is_retained(env):
    seed(env)
    add_index(env, 501, files=[100], status="building")
    with pytest.raises(cleanup.CleanupDeferred, match="快照"):
        run(env)
    assert version(env) is not None and not env.storage.deleted
    with env.factory.begin() as s:
        index = s.get(KbIndex, 501)
        index.status = "ready"
        index.created_at = NOW
        index.manifest_json = {"old_format": []}
    with pytest.raises(cleanup.CleanupDeferred, match="快照"):
        run(env)
    assert version(env) is not None and not env.storage.deleted


@pytest.mark.parametrize("bucket", ["isolated-bucket-a", "nlprag"])
def test_bucket_is_taken_from_configuration(env, bucket):
    env.settings.minio_bucket = bucket
    object_uri_value = seed(env)
    assert run(env)["objects_deleted"] == 1
    assert env.storage.deleted == [object_uri_value]


def test_bucket_mismatch_is_retained(env):
    seed(env)
    env.settings.minio_bucket = "another-isolated-bucket"
    with pytest.raises(cleanup.CleanupDeferred, match="URI"):
        run(env)
    assert version(env) is not None and guard(env)["state"] == "ready"
    assert not env.storage.deleted


@pytest.mark.parametrize("state", ["writing", "collision"])
def test_uncertain_guard_raises_without_delete_or_mutation(env, state):
    seed(env)
    with env.factory.begin() as s:
        values = {"state": "writing", "token": "a" * 32} if state == "writing" else {
            "uri": "s3://other-bucket/not-this-object"
        }
        s.execute(upload_object_guard.update().values(**values))
    with pytest.raises(cleanup.CleanupDeferred, match="屏障"):
        run(env)
    assert version(env) is not None and not env.storage.deleted


def test_legacy_object_without_guard_is_retained_without_error(env):
    # 缺行 = 迁移前遗留：保守保留、只计数，不删除、不抛错、不补建屏障。
    object_uri_value = seed(env, with_guard=False)
    result = run(env)
    assert result["versions_retained"] == 1 and result["documents_retained"] == 1
    assert result["objects_deleted"] == 0 and result["documents_purged"] == 0
    assert not env.storage.deleted and object_uri_value in env.storage.objects
    assert version(env) is not None and guard(env) is None
    assert not any(phase == "prepare" for _, phase in env.sessions.events)


def test_whole_kb_legacy_guard_retains_all_content(env):
    # 整库删除是原子预检：一个缺屏障版本即可否决全部物理删除。
    seed(env, with_guard=False, deleted=None, kb_deleted=OLD)
    add_index(env, 501, files=[100], active=True)
    result = run(env)
    assert result["kb_retained"] == 1 and result["versions_retained"] == 1
    assert result["indexes_removed"] == result["chunks_removed"] == 0
    with env.factory() as s:
        assert s.get(KbIndex, 501) is not None
        assert s.get(KbDocumentVersion, 100) is not None
        assert s.get(KbDocument, 10) is not None
    assert not env.storage.deleted


def test_whole_kb_mixed_guards_is_all_or_nothing(env):
    object_uri_value = seed(env, deleted=None, kb_deleted=OLD)
    shared_other = uri(env, "second.txt")
    with env.factory.begin() as s:
        s.add(KbDocument(id=11, kb_id=1, title="second"))
        s.flush()
        s.add(KbDocumentVersion(
            id=101, kb_id=1, document_id=11, version_no=1, title="second",
            file_sha256=b"y" * 32, storage_uri=shared_other,
        ))
    add_index(env, 501, files=[100, 101], active=True)
    result = run(env)  # 100 有屏障、101 缺失 → 整库原样保留
    assert result["versions_retained"] == 1 and result["kb_retained"] == 1
    assert result["objects_deleted"] == result["indexes_removed"] == 0
    assert version(env) is not None and version(env, 101) is not None
    assert not env.storage.deleted and object_uri_value in env.storage.objects


def test_uncertain_remote_delete_stays_blocked_even_on_retry(env):
    seed(env)
    env.storage.fail = True
    with pytest.raises(cleanup.CleanupDeferred, match="不确定") as error:
        run(env)
    assert not error.value.retryable
    assert guard(env)["state"] == "writing" and version(env) is not None
    assert len(env.storage.deleted) == 1
    env.storage.fail = False
    with pytest.raises(cleanup.CleanupDeferred, match="屏障"):
        run(env)
    assert len(env.storage.deleted) == 1


def test_same_key_upload_is_rejected_while_cleanup_delete_in_progress(env, monkeypatch):
    from app.api import documents
    from app.core.errors import Conflict
    from tests.unit.test_document_upload_offline import FakeDB, FakeStorage, upload

    env.settings.minio_bucket = "bucket"
    key = build_object_key(9, "report.pdf", local_date=dt.date(2026, 11, 7))
    shared = seed(env, uri_value=object_uri("bucket", key))
    real_build = documents.build_object_key
    monkeypatch.setattr(
        documents, "build_object_key",
        lambda uid, name: real_build(uid, name, local_date=dt.date(2026, 11, 7)),
    )
    db = FakeDB()
    upload_storage = FakeStorage(db)
    blocked = []

    def attempt_upload():
        row = dict(guard(env))
        db.data["guards"][row["uri_hash"]] = row
        with pytest.raises(Conflict, match="正在处理"):
            upload(documents, monkeypatch, db, upload_storage)
        assert not upload_storage.objects
        blocked.append(True)

    env.storage.on_delete = attempt_upload
    assert run(env)["objects_deleted"] == 1
    assert blocked == [True] and env.storage.deleted == [shared]


def test_concurrent_cleanup_cannot_pass_persistent_writing_guard(env):
    seed(env)
    observed = []

    def concurrent_attempt():
        with pytest.raises(cleanup.CleanupDeferred):
            run(env)
        observed.append(guard(env)["state"])

    env.storage.on_delete = concurrent_attempt
    assert run(env)["objects_deleted"] == 1
    assert observed == ["writing"] and len(env.storage.deleted) == 1


@pytest.mark.parametrize("phase,when", [
    ("prepare", "before"), ("prepare", "after"),
    ("final", "before"), ("final", "after"),
])
def test_ambiguous_commits_never_lose_both_guard_and_metadata(env, phase, when):
    seed(env)
    env.sessions.fail_commit = (phase, when)
    with pytest.raises(cleanup.CleanupDeferred, match="提交"):
        run(env)
    if phase == "prepare":
        assert not env.storage.deleted and version(env) is not None
        assert guard(env)["state"] == ("writing" if when == "after" else "ready")
    elif when == "before":
        assert len(env.storage.deleted) == 1
        assert guard(env)["state"] == "writing" and version(env) is not None
    else:
        assert len(env.storage.deleted) == 1
        assert guard(env)["state"] == "ready" and version(env) is None


def test_database_without_postgresql_locking_fails_closed(env):
    seed(env)
    with pytest.raises(cleanup.CleanupDeferred, match="PostgreSQL") as error:
        cleanup.cleanup_kb_scope(env.factory, env.settings, kb_id=1, now=NOW)
    assert not error.value.retryable
    assert version(env) is not None and not env.storage.deleted


def test_untrusted_storage_transport_fails_closed(env):
    seed(env)
    env.storage.safe_mutation_no_retry = False
    with pytest.raises(cleanup.CleanupDeferred, match="存储配置"):
        run(env)
    assert guard(env)["state"] == "ready" and version(env) is not None
    assert not env.storage.deleted


def test_active_job_and_retention_defer_instead_of_success(env):
    seed(env)
    with env.factory.begin() as s:
        s.add(IngestJob(
            id=300, kb_id=1, doc_version_id=100, job_type="ingest_file",
            status="running", payload_json={},
        ))
    with pytest.raises(cleanup.CleanupDeferred, match="未完成"):
        run(env)
    assert version(env) is not None and not env.storage.deleted
    with env.factory.begin() as s:
        s.get(IngestJob, 300).status = "succeeded"
        s.get(KbDocument, 10).deleted_at = NOW - dt.timedelta(hours=1)
    with pytest.raises(cleanup.CleanupDeferred, match="保留期") as error:
        run(env)
    assert error.value.retryable
    assert version(env) is not None and not env.storage.deleted


def test_building_snapshot_is_deferred_even_without_expired_documents(env):
    seed(env, deleted=None)
    add_index(env, 500, files=[100], status="building")
    with pytest.raises(cleanup.CleanupDeferred, match="构建中") as error:
        run(env)
    assert not error.value.retryable
    with env.factory() as s:
        assert s.get(KbIndex, 500) is not None


def test_active_job_defers_snapshot_even_without_expired_documents(env):
    seed(env, deleted=None)
    add_index(env, 500, files=[100])
    with env.factory.begin() as s:
        s.add(IngestJob(
            id=300, kb_id=1, job_type="reindex", status="queued", payload_json={},
        ))
    with pytest.raises(cleanup.CleanupDeferred, match="未完成"):
        run(env)
    with env.factory() as s:
        assert s.get(KbIndex, 500) is not None


def test_expired_kb_invalidates_all_documents_before_removing_any_version(env):
    seed(env, deleted=None, kb_deleted=OLD)
    with env.factory.begin() as s:
        s.add(KbDocument(id=20, kb_id=1, title="second"))
    seen = []

    def verify_invalidated():
        with env.factory() as s:
            assert s.get(KbDocument, 10).deleted_at is not None
            assert s.get(KbDocument, 20).deleted_at is not None
        seen.append(True)

    env.storage.on_delete = verify_invalidated
    result = run(env)
    assert seen == [True]
    assert result["objects_deleted"] == 1 and result["kb_retained"] == 1


def test_expired_kb_purges_protocol_content_but_retains_parent(env):
    object_uri_value = seed(env, deleted=None, kb_deleted=OLD)
    add_index(env, 501, files=[100], active=True)
    result = run(env)
    assert result["kb_retained"] == 1 and "kb_purged" not in result
    assert result["objects_deleted"] == 1 and result["documents_purged"] == 1
    assert result["indexes_removed"] == 1 and result["versions_retained"] == 0
    assert env.storage.deleted == [object_uri_value]
    with env.factory() as s:
        assert version(env) is None
        assert s.get(KbDocument, 10) is None
        assert s.get(KbIndex, 501) is None
        assert s.get(KbCollection, 1) is not None  # tombstone，父行保留
    assert guard(env)["state"] == "ready" and guard(env)["token"] is None
