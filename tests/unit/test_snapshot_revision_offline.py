"""修订号与快照并发的离线回归测试；不连接数据库或对象存储。"""

import copy
import operator
from types import SimpleNamespace

import pytest
from sqlalchemy.sql.dml import Update
from sqlalchemy.sql.elements import BinaryExpression
from sqlalchemy.sql.selectable import Select

from app.api import documents, kbs
from app.core.errors import Conflict
from app.db.models import IngestJob, KbCollection, KbDocument, KbDocumentVersion, KbIndex
from app.indexing import snapshot
from app.schemas.api import DocumentPatchRequest


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("离线测试不得连接网络")

    monkeypatch.setattr("socket.socket.connect", forbidden)
    monkeypatch.setattr("socket.socket.connect_ex", forbidden)
    monkeypatch.setattr("socket.create_connection", forbidden)


class Result:
    def __init__(self, value=None, rowcount=1):
        self.value = value
        self.rowcount = rowcount

    def scalar_one_or_none(self):
        return self.value

    def scalar_one(self):
        assert self.value is not None
        return self.value

    def all(self):
        return self.value


class FakeDatabase:
    def __init__(self):
        self.state = {
            "kb": SimpleNamespace(
                id=1, name="test", tenant_id=1, deleted_at=None, data_revision=2,
                acl_revision=3, active_index_id=None,
            ),
            "documents": {},
            "versions": {},
            "indexes": {},
            "jobs": [],
            "next_id": 100,
        }
        self.hook = None
        self.fail_revision_update = False
        self.commits = 0

    def session(self):
        return FakeSession(self)

    def add_file(self, number, status="pending"):
        self.state["documents"][number] = SimpleNamespace(
            id=number, kb_id=1, uploader_user_id=7, visibility="public",
            org_tag_id=None, deleted_at=None,
        )
        self.state["versions"][number] = SimpleNamespace(
            id=number, kb_id=1, document_id=number, title=f"file{number}",
            storage_uri=f"s3://bucket/2026/11/07/7/file{number}/file{number}.txt",
            file_sha256=b"x" * 32,
            parse_status=status,
        )


class FakeSession:
    """仅实现本测试使用的 SQL 形状，并在 commit 时原子提交内存状态。"""

    def __init__(self, db):
        self.db = db
        self.state = copy.deepcopy(db.state)
        self.loaded_kb = None
        self.loaded_documents = {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, model, ident):
        if model is KbCollection:
            self.loaded_kb = copy.deepcopy(self.state["kb"])
            return self.loaded_kb if ident == self.loaded_kb.id else None
        if model is KbDocument:
            obj = copy.deepcopy(self.state["documents"].get(ident))
            self.loaded_documents[ident] = obj
            return obj
        if model is KbDocumentVersion:
            return copy.deepcopy(self.state["versions"].get(ident))
        raise AssertionError(model)

    def execute(self, stmt):
        params = stmt.compile().params
        sql = str(stmt)
        if self.db.hook is not None:
            hook, self.db.hook = self.db.hook, None
            hook(self, stmt)
        if isinstance(stmt, Select):
            if "FROM kb_collection" in sql:
                assert "FOR UPDATE" in sql
                self.loaded_kb = copy.deepcopy(self.state["kb"])
                return Result(self.loaded_kb)
            if "FROM kb_document_version" in sql:
                assert "kb_document_version.parse_status" in sql
                return Result([
                    (copy.deepcopy(v), copy.deepcopy(self.state["documents"][v.document_id]))
                    for v in sorted(self.state["versions"].values(), key=lambda v: v.id)
                    if v.kb_id == self.state["kb"].id
                    and v.parse_status == params["parse_status_1"]
                    and self.state["documents"][v.document_id].deleted_at is None
                ])
            raise AssertionError(sql)
        if not isinstance(stmt, Update):
            raise AssertionError(sql)
        table = stmt.table.name
        if table == "kb_document_version":
            assert "kb_document_version.parse_status !=" in sql
            assert "RETURNING kb_document_version.kb_id" in sql
            version = self.state["versions"].get(params["id_1"])
            if version is None or version.parse_status == params["parse_status_1"]:
                return Result(None, rowcount=0)
            version.parse_status = params["parse_status"]
            return Result(version.kb_id)
        if table == "kb_collection":
            kb = self.state["kb"]
            assert params["id_1"] == kb.id
            values = {column.name: value for column, value in stmt._values.items()}
            if "data_revision" in values:
                assert_atomic_increment(stmt, "data_revision")
                if self.db.fail_revision_update:
                    raise RuntimeError("simulated revision failure")
                if "deleted_at" in values:
                    assert "kb_collection.deleted_at IS NULL" in sql
                    if kb.deleted_at is not None:
                        return Result(rowcount=0)
                    kb.deleted_at = params["deleted_at"]
                kb.data_revision += 1
            else:
                assert_atomic_increment(stmt, "acl_revision")
                kb.acl_revision += 1
            return Result()
        if table == "kb_document":
            assert "kb_document.deleted_at IS NULL" in sql
            doc = self.state["documents"].get(params["id_1"])
            if doc is None or doc.deleted_at is not None:
                return Result(rowcount=0)
            doc.deleted_at = params["deleted_at"]
            return Result()
        if table == "kb_chunk":
            return Result()
        if table == "kb_index":
            if "id_1" not in params:
                assert "kb_index.status =" in sql
                for index in self.state["indexes"].values():
                    if index.kb_id == params["kb_id_1"] and index.status == params["status_1"]:
                        index.status = params["status"]
                return Result()
            index = self.state["indexes"].get(params["id_1"])
            assert "kb_index.kb_id =" in sql and "kb_index.status =" in sql
            if (
                index is None or index.kb_id != params["kb_id_1"]
                or index.status != params["status_1"]
                or ("source_revision_1" in params
                    and index.source_revision != params["source_revision_1"])
            ):
                return Result(rowcount=0)
            index.status = params["status"]
            return Result()
        raise AssertionError(sql)

    def add(self, obj):
        self.state["next_id"] += 1
        obj.id = self.state["next_id"]
        if isinstance(obj, KbIndex):
            self.state["indexes"][obj.id] = SimpleNamespace(
                id=obj.id, kb_id=obj.kb_id, status=obj.status,
                source_revision=obj.source_revision, manifest_json=copy.deepcopy(obj.manifest_json),
            )
        elif isinstance(obj, IngestJob):
            self.state["jobs"].append(SimpleNamespace(id=obj.id, kb_id=obj.kb_id))
        else:
            raise AssertionError(type(obj))

    def add_all(self, objects):
        for obj in objects:
            self.add(obj)

    def commit(self):
        if self.loaded_kb is not None:
            # 模拟 ORM 仅刷新显式修改的 active_index_id，而不是覆盖修订号。
            self.state["kb"].active_index_id = self.loaded_kb.active_index_id
        self.db.state = copy.deepcopy(self.state)
        self.db.commits += 1


def assert_atomic_increment(stmt, field):
    expression = next(value for column, value in stmt._values.items() if column.name == field)
    assert isinstance(expression, BinaryExpression)
    assert expression.operator is operator.add
    assert expression.left.compare(getattr(KbCollection.__table__.c, field))
    assert expression.right.value == 1


def builder(db, test_settings):
    return snapshot.IndexBuilder(db.session, None, test_settings)


def principal():
    return SimpleNamespace(user_id=7, can_see_all=lambda: True)


def test_parse_status_changes_bump_revision_once_and_rollback(test_settings):
    db = FakeDatabase()
    db.add_file(1)
    indexer = builder(db, test_settings)
    indexer._mark_parse_status(1, "succeeded")
    assert db.state["kb"].data_revision == 3
    indexer._mark_parse_status(1, "succeeded")
    assert db.state["kb"].data_revision == 3
    for revision, status in enumerate(("needs_review", "failed", "pending"), start=4):
        indexer._mark_parse_status(1, status)
        assert db.state["kb"].data_revision == revision
        assert db.state["versions"][1].parse_status == status
    indexer._mark_parse_status(999, "failed")
    assert db.state["kb"].data_revision == 6
    db.fail_revision_update = True
    with pytest.raises(RuntimeError, match="revision failure"):
        indexer._mark_parse_status(1, "succeeded")
    assert db.state["versions"][1].parse_status == "pending"
    assert db.state["kb"].data_revision == 6


def test_old_manifest_cannot_supersede_new_build(test_settings):
    db = FakeDatabase()
    indexer = builder(db, test_settings)
    db.state["kb"].data_revision = 3
    newer = indexer._create_index_row(1, 3, [], 1)
    with pytest.raises(snapshot.RevisionChanged):
        indexer._create_index_row(1, 2, [], 1)
    assert db.state["indexes"][newer].status == "building"
    indexer._publish(1, newer, 3)
    assert db.state["kb"].active_index_id == newer


def test_manifest_read_overlapping_parse_change_is_rejected(test_settings):
    db = FakeDatabase()
    db.add_file(1)
    test_settings.minio_bucket = "bucket"
    indexer = builder(db, test_settings)

    def parse_before_manifest(session, stmt):
        assert isinstance(stmt, Select) and "FROM kb_document_version" in str(stmt)
        indexer._mark_parse_status(1, "succeeded")
        # READ COMMITTED 下第二条语句可见已提交的解析状态，但构建仍持有旧修订号。
        session.state = copy.deepcopy(db.state)

    db.hook = parse_before_manifest
    build = indexer.build(kb_id=1)
    with pytest.raises(snapshot.RevisionChanged):
        build.send(None)
    assert db.state["kb"].data_revision == 3
    assert db.state["versions"][1].parse_status == "succeeded"
    assert not db.state["indexes"]


def test_superseded_and_foreign_snapshots_never_publish(test_settings):
    db = FakeDatabase()
    indexer = builder(db, test_settings)
    first = indexer._create_index_row(1, 2, [], 1)
    second = indexer._create_index_row(1, 2, [], 1)
    with pytest.raises(snapshot.RevisionChanged):
        indexer._publish(1, first, 2)
    assert db.state["indexes"][first].status == "superseded"
    assert db.state["kb"].active_index_id is None
    indexer._publish(1, second, 2)
    with pytest.raises(snapshot.RevisionChanged):
        indexer._publish(1, first, 2)
    assert db.state["kb"].active_index_id == second
    third = indexer._create_index_row(1, 2, [], 1)
    db.state["indexes"][third].source_revision = 1
    with pytest.raises(snapshot.RevisionChanged):
        indexer._publish(1, third, 2)
    assert db.state["indexes"][third].status == "building"
    assert db.state["kb"].active_index_id == second
    db.state["indexes"][first].kb_id = 9
    db.state["indexes"][first].status = "building"
    with pytest.raises(snapshot.RevisionChanged):
        indexer._publish(1, first, 2)
    assert db.state["indexes"][first].status == "building"
    assert db.state["kb"].active_index_id == second


def test_deleted_kb_cannot_create_or_publish(test_settings):
    db = FakeDatabase()
    indexer = builder(db, test_settings)
    index = indexer._create_index_row(1, 2, [], 1)
    db.state["kb"].deleted_at = object()
    with pytest.raises(snapshot.RevisionChanged):
        indexer._create_index_row(1, 2, [], 1)
    with pytest.raises(snapshot.RevisionChanged):
        indexer._publish(1, index, 2)
    assert db.state["indexes"][index].status == "superseded"
    assert db.state["kb"].active_index_id is None


def test_interleaved_parses_publish_both_files(monkeypatch, test_settings):
    db = FakeDatabase()
    db.add_file(1)
    db.add_file(2)
    test_settings.minio_bucket = "bucket"
    class Embedding:
        async def embed_texts(self, inputs, **kwargs):
            await Pause()
            return SimpleNamespace(usage=snapshot.TokenUsage(), cache_hits=0, vectors=[])

    class Pause:
        def __await__(self):
            yield "embedding"

    indexer = snapshot.IndexBuilder(db.session, Embedding(), test_settings)
    monkeypatch.setattr(indexer, "_load_verified", lambda *args: b"content")
    monkeypatch.setattr(snapshot, "parse_upload", lambda *args: SimpleNamespace(quality=None))
    monkeypatch.setattr(
        snapshot, "run_pipeline",
        lambda *args: SimpleNamespace(chunks=[SimpleNamespace(embedding_input="chunk")]),
    )
    monkeypatch.setattr(indexer, "_insert_chunks", lambda *args: None)
    monkeypatch.setattr(indexer, "_validate", lambda *args: None)
    monkeypatch.setattr(indexer, "_store_manifest", lambda *args: None)

    first = indexer.build(kb_id=1, new_doc_version_id=1)
    assert first.send(None) == "embedding"
    first_index = next(iter(db.state["indexes"].values()))
    assert first_index.source_revision == 3
    assert first_index.manifest_json["files"] == [1]
    second = indexer.build(kb_id=1, new_doc_version_id=2)
    assert second.send(None) == "embedding"
    with pytest.raises(StopIteration) as completed:
        second.send(None)
    report = completed.value.value
    assert report.files == 2
    assert db.state["indexes"][report.index_id].source_revision == 4
    assert db.state["indexes"][report.index_id].manifest_json["files"] == [1, 2]
    with pytest.raises(snapshot.RevisionChanged):
        first.send(None)
    assert db.state["indexes"][first_index.id].status == "superseded"
    assert db.state["kb"].active_index_id == report.index_id


def test_document_delete_preserves_concurrent_replacement_increment():
    db = FakeDatabase()
    db.add_file(1, "succeeded")

    def replacement(session, stmt):
        assert isinstance(stmt, Update) and stmt.table.name == "kb_document"
        # 同键替换在本请求读文档后先提交了另一个知识库修订。
        session.state["kb"].data_revision += 1
        db.state["kb"].data_revision += 1

    db.hook = replacement
    result = kbs.delete_document(1, db.session(), principal())
    assert result.document_id == 1
    assert db.state["documents"][1].deleted_at is not None
    assert db.state["kb"].data_revision == 4
    assert len(db.state["jobs"]) == 2


def test_document_delete_does_not_duplicate_a_concurrent_delete():
    db = FakeDatabase()
    db.add_file(1)

    def other_delete(session, stmt):
        session.state["documents"][1].deleted_at = "already deleted"
        db.state["documents"][1].deleted_at = "already deleted"
        session.state["kb"].data_revision += 1
        db.state["kb"].data_revision += 1

    db.hook = other_delete
    with pytest.raises(Conflict):
        kbs.delete_document(1, db.session(), principal())
    assert db.state["kb"].data_revision == 3
    assert not db.state["jobs"]


def test_kb_delete_preserves_concurrent_revision(monkeypatch):
    db = FakeDatabase()
    session = db.session()
    monkeypatch.setattr(kbs, "require_kb_access", lambda **kwargs: session.get(KbCollection, 1))

    def replacement(session, stmt):
        session.state["kb"].data_revision += 1
        db.state["kb"].data_revision += 1

    db.hook = replacement
    result = kbs.delete_kb(1, session, principal())
    assert result.kb_id == 1
    assert db.state["kb"].deleted_at is not None
    assert db.state["kb"].data_revision == 4


def test_acl_patch_preserves_concurrent_revision(monkeypatch):
    db = FakeDatabase()
    db.add_file(1)
    monkeypatch.setattr(documents, "_load_document_row", lambda *args: ("row",))
    monkeypatch.setattr(documents, "_document_item", lambda *args: "patched")

    def other_acl_change(session, stmt):
        assert isinstance(stmt, Update) and stmt.table.name == "kb_chunk"
        session.state["kb"].acl_revision += 1
        db.state["kb"].acl_revision += 1

    db.hook = other_acl_change
    result = documents.update_document(
        1, DocumentPatchRequest(visibility="private"), db.session(), principal()
    )
    assert result == "patched"
    assert db.state["kb"].acl_revision == 5


def test_shared_fake_minio_fixture_is_compatible(fake_minio, test_settings):
    uri, digest = fake_minio.seed(test_settings, "fixture.txt", b"fixture")
    assert fake_minio.contains(uri)
    assert len(digest) == 32
    assert fake_minio.storage(test_settings).get(uri) == b"fixture"


def test_old_upload_endpoint_offloads_entire_call_without_sharing_session(monkeypatch):
    session = object()
    kb = SimpleNamespace(id=1)
    user = SimpleNamespace(org_tag_ids={2})
    settings = object()
    response = object()
    events = []
    class Pause:
        def __await__(self):
            yield "threadpool paused"

    async def file_read():
        events.append("read")
        return b"content"

    file = SimpleNamespace(filename="report.txt", content_type="text/plain", read=file_read)

    def access(**kwargs):
        assert kwargs["session"] is session
        events.append("access")
        return kb

    def create(**kwargs):
        assert kwargs == {
            "session": session, "principal": user, "settings": settings, "kb": kb,
            "org_tag_id": 2, "filename": "report.txt", "content_type": "text/plain",
            "payload": b"content", "fmt": "txt", "visibility": "private",
            "idempotency_key": "test-key", "route": "documents:1",
        }
        events.append("create")
        return response

    async def threadpool(fn, **kwargs):
        assert fn is create
        events.append("threadpool entered")
        await Pause()
        result = fn(**kwargs)
        events.append("threadpool returned")
        return result

    monkeypatch.setattr(kbs, "require_kb_access", access)
    monkeypatch.setattr(kbs, "check_size", lambda *args: None)
    monkeypatch.setattr(kbs, "detect_format", lambda *args: "txt")
    monkeypatch.setattr(kbs, "create_document_record", create)
    monkeypatch.setattr(kbs, "run_in_threadpool", threadpool)

    upload = kbs.upload_document(1, session, user, settings, file, "private", "test-key")
    assert upload.send(None) == "threadpool paused"
    assert events == ["access", "read", "threadpool entered"]
    with pytest.raises(StopIteration) as completed:
        upload.send(None)
    assert completed.value.value is response
    assert events == ["access", "read", "threadpool entered", "create", "threadpool returned"]
