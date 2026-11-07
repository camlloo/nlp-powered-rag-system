"""上传屏障离线单测：所有 DB 与对象存储操作均使用内存替身。"""

import copy
import importlib
import importlib.util
import json
import sys
import types
from datetime import date
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from sqlalchemy.sql.dml import Delete, Insert, Update
from sqlalchemy.sql.selectable import Select

from app.core.auth import Principal
from app.core.errors import AppError, Conflict
from app.db.models import IngestJob, KbDocument, KbDocumentVersion


@pytest.fixture
def documents(monkeypatch):
    # 开发环境可以没有 MinIO SDK；测试绝不实例化真实客户端。
    missing = importlib.util.find_spec("minio") is None
    if missing:
        minio = types.ModuleType("minio")
        minio.Minio = object
        error = types.ModuleType("minio.error")
        error.S3Error = type("S3Error", (Exception,), {})
        monkeypatch.setitem(sys.modules, "minio", minio)
        monkeypatch.setitem(sys.modules, "minio.error", error)
    module = importlib.import_module("app.api.documents")
    real_build = module.build_object_key
    monkeypatch.setattr(
        module, "build_object_key",
        lambda uid, name: real_build(uid, name, local_date=date(2026, 11, 7)),
    )
    yield module
    if missing:
        sys.modules.pop("app.api.documents", None)
        sys.modules.pop("app.storage", None)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("离线测试不得连接网络")

    monkeypatch.setattr("socket.socket.connect", forbidden)
    monkeypatch.setattr("socket.socket.connect_ex", forbidden)
    monkeypatch.setattr("socket.create_connection", forbidden)


class Result:
    def __init__(self, value=None, *, rowcount=1):
        self.value = value
        self.rowcount = rowcount

    def mappings(self):
        return self

    def one(self):
        assert self.value is not None
        return self.value

    def one_or_none(self):
        return self.value

    def scalars(self):
        return self

    def all(self):
        return self.value


class FakeDB:
    def __init__(self):
        self.data = {
            "guards": {}, "idem": {}, "documents": [], "versions": [], "jobs": [],
            "revisions": {1: 5, 2: 8}, "kbs": {1: None, 2: None},
        }
        self.events = []
        self.next_id = 100
        self.late_kb_delete = False


class FakeSession:
    def __init__(self, db, *, fail_commit=None, ambiguous=False):
        self.db = db
        self.data = copy.deepcopy(db.data)
        self.fail_commit = fail_commit
        self.ambiguous = ambiguous
        self.commit_count = 0

    def get_bind(self):
        return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    def scalar(self, stmt):
        assert str(stmt) == "SHOW transaction_isolation"
        return "read committed"

    def execute(self, stmt):
        params = stmt.compile().params
        sql = str(stmt)
        if isinstance(stmt, Insert):
            if stmt.table.name == "upload_idempotency":
                self.data["idem"].setdefault(params["identity_hash"], params.copy())
            elif stmt.table.name == "upload_object_guard":
                self.data["guards"].setdefault(params["uri_hash"], params.copy())
            else:
                raise AssertionError(sql)
            return Result()
        if isinstance(stmt, Select):
            assert "FOR UPDATE" in sql
            if "upload_idempotency" in sql:
                return Result(self.data["idem"].get(params["identity_hash_1"]))
            if "upload_object_guard" in sql:
                return Result(self.data["guards"].get(params["uri_hash_1"]))
            if "kb_collection" in sql:
                kb_id = params["id_1"]
                if kb_id not in self.data["kbs"]:
                    return Result(None)
                deleted = "deleted" if self.db.late_kb_delete and self.commit_count else None
                return Result((kb_id, self.data["kbs"][kb_id] or deleted))
            if "kb_document" in sql:
                uri = params["storage_uri_1"]
                ids = {v["document_id"] for v in self.data["versions"] if v["uri"] == uri}
                return Result(sorted(
                    d["id"] for d in self.data["documents"] if d["id"] in ids and not d["deleted"]
                ))
            raise AssertionError(sql)
        if isinstance(stmt, Delete):
            assert "upload_idempotency" in sql
            self.data["idem"].pop(params["identity_hash_1"], None)
            return Result(rowcount=1)
        if isinstance(stmt, Update):
            name = stmt.table.name
            if name == "upload_object_guard":
                row = self.data["guards"][params["uri_hash_1"]]
                if "token_1" in params and row["token"] != params["token_1"]:
                    return Result(rowcount=0)
                row.update(state=params["state"], token=params["token"])
            elif name == "upload_idempotency":
                row = self.data["idem"][params["identity_hash_1"]]
                if row["token"] != params["token_1"]:
                    return Result(rowcount=0)
                row.update({k: v for k, v in params.items() if k in row or k in (
                    "job_id", "document_id", "version_no", "kb_id"
                )})
            elif name == "kb_document":
                # 确定性替换失效：按加锁清单标记并返回受影响的 KB。
                stale_ids = set()
                for key, value in params.items():
                    if key.startswith("id_"):
                        stale_ids.update(value if isinstance(value, list) else [value])
                affected = []
                for doc in self.data["documents"]:
                    if doc["id"] in stale_ids and not doc["deleted"]:
                        doc["deleted"] = True
                        affected.append(doc["kb_id"])
                return Result(affected)
            elif name == "kb_collection":
                self.data["revisions"][params["id_1"]] += 1
            else:
                raise AssertionError(sql)
            return Result()
        raise AssertionError(sql)

    def add(self, obj):
        self.db.next_id += 1
        obj.id = self.db.next_id
        if isinstance(obj, KbDocument):
            self.data["documents"].append({
                "id": obj.id, "kb_id": obj.kb_id, "deleted": False,
                "uploader": obj.uploader_user_id, "visibility": obj.visibility,
                "org_tag_id": obj.org_tag_id, "title": obj.title,
            })
        elif isinstance(obj, KbDocumentVersion):
            self.data["versions"].append({
                "id": obj.id, "document_id": obj.document_id, "uri": obj.storage_uri,
                "digest": obj.file_sha256,
            })
        elif isinstance(obj, IngestJob):
            self.data["jobs"].append({
                "id": obj.id, "kb_id": obj.kb_id, "type": obj.job_type,
                "payload": obj.payload_json,
            })
        else:
            raise AssertionError(type(obj))

    def flush(self):
        pass

    def commit(self):
        self.commit_count += 1
        self.db.events.append("commit")
        if self.commit_count == self.fail_commit:
            if self.ambiguous:
                self.db.data = copy.deepcopy(self.data)
            raise RuntimeError("simulated commit failure")
        self.db.data = copy.deepcopy(self.data)

    def rollback(self):
        self.db.events.append("rollback")
        self.data = copy.deepcopy(self.db.data)


class FakeStorage:
    safe_mutation_no_retry = True

    def __init__(self, db, *, fail=None):
        self.db = db
        self.fail = fail
        self.objects = {}

    def delete(self, uri):
        self.db.events.append("delete")
        guard = next(g for g in self.db.data["guards"].values() if g["uri"] == uri)
        assert guard["state"] == "writing"
        assert all(d["deleted"] for d in self.db.data["documents"] if d["id"] < 100)
        if self.fail == "delete":
            raise TimeoutError()
        self.objects.pop(uri, None)

    def put(self, key, payload, *, content_type):
        self.db.events.append("put")
        if self.fail == "put":
            raise TimeoutError()
        uri = "s3://bucket/" + quote(key, safe="/")
        self.objects[uri] = (payload, content_type)
        return uri


def upload(documents, monkeypatch, db, storage, *, session=None, **changes):
    monkeypatch.setattr(documents, "MinioStorage", lambda settings: storage)
    args = {
        "session": session or FakeSession(db),
        "principal": Principal(tenant_id=1, user_id=9, username="test"),
        "settings": SimpleNamespace(minio_bucket="bucket"),
        "kb": SimpleNamespace(id=1), "org_tag_id": 3, "filename": "report.pdf",
        "content_type": "application/pdf", "payload": b"new data", "fmt": "pdf",
        "visibility": "private", "idempotency_key": None, "route": "documents:main",
    }
    args.update(changes)
    return documents.create_document_record(**args)


def seed_multiple_versions(documents, db):
    uri = documents.object_uri("bucket", "2026/11/07/9/report/report.pdf")
    db.data["documents"] = [
        {"id": 1, "kb_id": 1, "deleted": False},
        {"id": 2, "kb_id": 2, "deleted": False},
    ]
    # 旧版本和当前版本皆需触发整篇文档失效。
    db.data["versions"] = [
        {"id": 11, "document_id": 1, "uri": uri},
        {"id": 12, "document_id": 2, "uri": uri},
        {"id": 13, "document_id": 2, "uri": "s3://bucket/other"},
    ]
    return uri


def test_unsafe_storage_without_retry_guarantee_fails_closed(documents, monkeypatch):
    db = FakeDB()
    seed_multiple_versions(documents, db)
    storage = FakeStorage(db)
    storage.safe_mutation_no_retry = False
    with pytest.raises(AppError, match="不支持安全上传") as caught:
        upload(documents, monkeypatch, db, storage, idempotency_key="k")
    assert not caught.value.retryable
    assert db.events == ["rollback"]
    assert not db.data["guards"] and not db.data["idem"]
    assert not any(d["deleted"] for d in db.data["documents"])


def test_replacement_commits_all_invalidation_before_delete(documents, monkeypatch):
    db = FakeDB()
    uri = seed_multiple_versions(documents, db)
    storage = FakeStorage(db)
    result = upload(documents, monkeypatch, db, storage)
    assert db.events == ["commit", "delete", "put", "commit"]
    assert result.document_id == db.data["documents"][-1]["id"] and not result.duplicate
    assert [d["deleted"] for d in db.data["documents"]] == [True, True, False]
    assert db.data["revisions"] == {1: 7, 2: 9}
    assert [j["type"] for j in db.data["jobs"]] == ["reindex", "reindex", "ingest_file"]
    assert db.data["versions"][-1]["uri"] == uri
    assert db.data["documents"][-1]["org_tag_id"] == 3
    assert db.data["documents"][-1]["visibility"] == "private"
    assert db.data["jobs"][-1]["payload"]["filename"] == "report.pdf"
    assert next(iter(db.data["guards"].values()))["state"] == "ready"


@pytest.mark.parametrize("failure", ["delete", "put"])
def test_uncertain_storage_failure_never_compensates_or_retries(documents, monkeypatch, failure):
    db = FakeDB()
    seed_multiple_versions(documents, db)
    storage = FakeStorage(db, fail=failure)
    with pytest.raises(AppError, match="不确定") as caught:
        upload(documents, monkeypatch, db, storage, idempotency_key="k")
    assert not caught.value.retryable
    expected = ["commit", "delete"] if failure == "delete" else ["commit", "delete", "put"]
    assert db.events == expected
    assert all(d["deleted"] for d in db.data["documents"])
    assert next(iter(db.data["guards"].values()))["state"] == "writing"
    storage.fail = None
    with pytest.raises(Conflict, match="结果不确定"):
        upload(documents, monkeypatch, db, storage, idempotency_key="k")
    with pytest.raises(Conflict, match="结果不确定"):
        upload(documents, monkeypatch, db, storage)
    assert db.events.count("delete") == 1
    assert db.events.count("put") == (failure == "put")


def test_db_failure_before_io_and_after_io(documents, monkeypatch):
    for failed_commit, expected in [(1, ["commit", "rollback"]),
                                    (2, ["commit", "delete", "put", "commit", "rollback"])]:
        db = FakeDB()
        seed_multiple_versions(documents, db)
        storage = FakeStorage(db)
        with pytest.raises(AppError) as caught:
            upload(documents, monkeypatch, db, storage,
                   session=FakeSession(db, fail_commit=failed_commit))
        assert not caught.value.retryable
        assert db.events == expected
        if failed_commit == 1:
            assert not db.data["guards"]
            assert not any(d["deleted"] for d in db.data["documents"])
        else:
            assert next(iter(db.data["guards"].values()))["state"] == "writing"
            assert all(d["deleted"] for d in db.data["documents"])
            assert len(db.data["versions"]) == 3


def test_ambiguous_first_commit_must_not_start_storage(documents, monkeypatch):
    db = FakeDB()
    seed_multiple_versions(documents, db)
    storage = FakeStorage(db)
    with pytest.raises(AppError, match="未发起对象写入"):
        upload(documents, monkeypatch, db, storage,
               session=FakeSession(db, fail_commit=1, ambiguous=True), idempotency_key="k")
    assert db.events == ["commit", "rollback"]
    assert next(iter(db.data["guards"].values()))["state"] == "writing"
    assert all(d["deleted"] for d in db.data["documents"])
    with pytest.raises(Conflict):
        upload(documents, monkeypatch, db, storage)
    assert "delete" not in db.events


def test_ambiguous_final_commit_is_replayed_not_compensated(documents, monkeypatch):
    db = FakeDB()
    storage = FakeStorage(db)
    with pytest.raises(AppError, match="数据库登记结果不确定"):
        upload(documents, monkeypatch, db, storage,
               session=FakeSession(db, fail_commit=2, ambiguous=True), idempotency_key="k")
    assert next(iter(db.data["guards"].values()))["state"] == "ready"
    result = upload(documents, monkeypatch, db, storage, idempotency_key="k")
    assert result.headers["Idempotent-Replay"] == "true"
    assert db.events.count("delete") == db.events.count("put") == 1


def test_simultaneous_same_key_is_blocked_before_first_put_returns(documents, monkeypatch):
    db = FakeDB()

    class InterleavedStorage(FakeStorage):
        def put(self, key, payload, *, content_type):
            with pytest.raises(Conflict, match="正在处理"):
                upload(documents, monkeypatch, db, self, payload=b"competing")
            return super().put(key, payload, content_type=content_type)

    storage = InterleavedStorage(db)
    upload(documents, monkeypatch, db, storage)
    assert db.events.count("delete") == 1
    assert db.events.count("put") == 1
    assert len(db.data["documents"]) == 1
    assert next(iter(db.data["guards"].values()))["state"] == "ready"


def test_unicode_filename_kept_exactly(documents, monkeypatch):
    db = FakeDB()
    storage = FakeStorage(db)
    upload(documents, monkeypatch, db, storage, filename="报告.pdf")
    assert db.data["versions"][-1]["uri"] == documents.object_uri(
        "bucket", "2026/11/07/9/报告/报告.pdf"
    )
    assert db.data["documents"][-1]["title"] == "报告"
    assert db.data["jobs"][-1]["payload"]["filename"] == "报告.pdf"


def test_idempotent_replay_and_all_request_fields_are_bound(documents, monkeypatch):
    db = FakeDB()
    storage = FakeStorage(db)
    first = upload(documents, monkeypatch, db, storage, idempotency_key="key")
    replay = upload(documents, monkeypatch, db, storage, idempotency_key="key")
    assert replay.headers["Idempotent-Replay"] == "true"
    assert json.loads(replay.body)["document_id"] == first.document_id
    for change in (
        {"visibility": "public"}, {"org_tag_id": None}, {"filename": "other.pdf"},
        {"content_type": "text/plain"}, {"payload": b"different"}, {"fmt": "txt"},
        {"kb": SimpleNamespace(id=2)},
    ):
        with pytest.raises(Conflict):
            upload(documents, monkeypatch, db, storage, idempotency_key="key", **change)
    assert db.events.count("delete") == db.events.count("put") == 1


def test_same_bytes_with_different_scope_or_filename_is_not_deduplicated(documents, monkeypatch):
    db = FakeDB()
    storage = FakeStorage(db)
    first = upload(documents, monkeypatch, db, storage)
    second = upload(documents, monkeypatch, db, storage, filename="other.pdf", org_tag_id=None)
    assert first.document_id != second.document_id
    assert db.events.count("put") == 2
    assert db.data["documents"][-1]["org_tag_id"] is None


def test_kb_deleted_before_prepare_blocks_without_barrier(documents, monkeypatch):
    db = FakeDB()
    seed_multiple_versions(documents, db)
    db.data["kbs"][1] = "deleted"
    storage = FakeStorage(db)
    with pytest.raises(Conflict, match="知识库不存在或已删除"):
        upload(documents, monkeypatch, db, storage, idempotency_key="k")
    assert db.events == ["rollback"]
    assert not db.data["guards"] and not db.data["idem"]
    assert not any(d["deleted"] for d in db.data["documents"])
    assert "delete" not in db.events and "put" not in db.events


def test_kb_deleted_during_storage_io_releases_barrier_without_registration(
    documents, monkeypatch
):
    db = FakeDB()

    class LateDeleteStorage(FakeStorage):
        def put(self, key, payload, *, content_type):
            result = super().put(key, payload, content_type=content_type)
            db.late_kb_delete = True
            return result

    storage = LateDeleteStorage(db)
    with pytest.raises(AppError, match="已在上传期间删除") as caught:
        upload(documents, monkeypatch, db, storage, idempotency_key="k")
    assert caught.value.status_code == 409
    # 释放事务已提交后外层仍会回滚收尾（无副作用），事件序列含一次收尾 rollback。
    assert db.events == ["commit", "delete", "put", "commit", "rollback"]
    assert not db.data["documents"] and not db.data["versions"]
    assert not db.data["idem"]
    assert next(iter(db.data["guards"].values()))["state"] == "ready"
