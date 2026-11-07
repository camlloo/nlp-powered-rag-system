"""运行时索引缓存装载集成测试（实现文档 §3.2、附录 B1）。"""

import asyncio

import numpy as np
import pytest
from sqlalchemy import update

from app.core.config import Settings
from app.core.errors import Conflict
from app.db.models import KbCollection, KbIndex
from app.db.session import SessionLocal
from app.gateways.mock import MockEmbeddingGateway
from app.indexing.cache import IndexCacheManager
from app.indexing.snapshot import IndexBuilder
from tests.integration.helpers import cleanup_kb, create_kb

CONTENT = "第三章 费用报销。员工应在出差结束后 30 日内申请。交通费凭有效票据报销。".encode()


@pytest.fixture
def env(pg_ready, test_settings):
    kb_id = create_kb()
    settings = test_settings
    builder = IndexBuilder(SessionLocal, MockEmbeddingGateway(dim=1024), settings, worker_id="tw")
    yield kb_id, settings, builder
    cleanup_kb(kb_id)


def upload_and_build(kb_id: int, settings: Settings, builder: IndexBuilder) -> int:
    import hashlib

    from app.db.models import KbDocument, KbDocumentVersion
    from app.parsers.intake import MinioStorage
    from app.storage import build_object_key

    uri = MinioStorage(settings).put(build_object_key(1, "制度.txt"), CONTENT)
    digest = hashlib.sha256(CONTENT).digest()
    with SessionLocal() as session:
        document = KbDocument(kb_id=kb_id, title="制度")
        session.add(document)
        session.flush()
        version = KbDocumentVersion(
            kb_id=kb_id,
            document_id=document.id,
            version_no=1,
            title="制度",
            file_sha256=digest,
            storage_uri=uri,
            parse_status="pending",
        )
        session.add(version)
        session.commit()
        dvid = version.id
    asyncio.run(builder.build(kb_id=kb_id, new_doc_version_id=dvid))
    with SessionLocal() as session:
        return session.get(KbCollection, kb_id).active_index_id


def kb_row(kb_id: int) -> KbCollection:
    with SessionLocal() as session:
        kb = session.get(KbCollection, kb_id)
        session.expunge(kb)
        return kb


def test_load_alignment_and_reuse(env):
    kb_id, settings, builder = env
    index_id = upload_and_build(kb_id, settings, builder)
    manager = IndexCacheManager(settings)
    kb = kb_row(kb_id)

    loaded = asyncio.run(
        manager.ensure_loaded(
            session_factory=SessionLocal,
            kb_id=kb_id,
            tenant_id=kb.tenant_id,
            expected_index_id=index_id,
            expected_data_revision=kb.data_revision,
            expected_acl_revision=kb.acl_revision,
        )
    )
    assert loaded.chunk_ids == sorted(loaded.chunk_ids)
    assert loaded.matrix.shape == (len(loaded.chunk_ids), 1024)
    assert loaded.records[loaded.chunk_ids[0]].content
    assert np.isfinite(loaded.matrix).all()
    norms = np.linalg.norm(loaded.matrix, axis=1)
    assert np.allclose(norms, 1.0, atol=1e-5)

    again = asyncio.run(
        manager.ensure_loaded(
            session_factory=SessionLocal,
            kb_id=kb_id,
            tenant_id=kb.tenant_id,
            expected_index_id=index_id,
            expected_data_revision=kb.data_revision,
            expected_acl_revision=kb.acl_revision,
        )
    )
    assert again is loaded  # 修订未变：直接复用缓存


def test_revision_change_forces_reload(env):
    kb_id, settings, builder = env
    index_id = upload_and_build(kb_id, settings, builder)
    manager = IndexCacheManager(settings)
    kb = kb_row(kb_id)

    asyncio.run(
        manager.ensure_loaded(
            session_factory=SessionLocal, kb_id=kb_id, tenant_id=kb.tenant_id,
            expected_index_id=index_id, expected_data_revision=kb.data_revision,
            expected_acl_revision=kb.acl_revision,
        )
    )
    # 修订号变化（如删除文件）：缓存不匹配，重新装载同一快照
    with SessionLocal() as session:
        session.execute(
            update(KbCollection).where(KbCollection.id == kb_id).values(
                data_revision=KbCollection.data_revision + 1
            )
        )
        session.commit()
    kb2 = kb_row(kb_id)
    reloaded = asyncio.run(
        manager.ensure_loaded(
            session_factory=SessionLocal, kb_id=kb_id, tenant_id=kb.tenant_id,
            expected_index_id=index_id, expected_data_revision=kb2.data_revision,
            expected_acl_revision=kb2.acl_revision,
        )
    )
    assert reloaded is not None and reloaded.data_revision == kb2.data_revision


def test_load_race_conflict(env):
    kb_id, settings, builder = env
    upload_and_build(kb_id, settings, builder)
    manager = IndexCacheManager(settings)
    kb = kb_row(kb_id)

    # 构造"装载目标与库内 active 不一致"的竞态：指向另一个 ready 快照
    with SessionLocal() as session:
        ghost = KbIndex(
            kb_id=kb_id,
            source_revision=kb.data_revision,
            status="ready",
            embedding_model="text-embedding-v4",
            embedding_dim=1024,
            pipeline_version="ghost",
            config_json={},
            manifest_json={},
        )
        session.add(ghost)
        session.commit()
        ghost_id = ghost.id

    with pytest.raises(Conflict, match="发生变化"):
        asyncio.run(
            manager.ensure_loaded(
                session_factory=SessionLocal, kb_id=kb_id, tenant_id=kb.tenant_id,
                expected_index_id=ghost_id, expected_data_revision=kb.data_revision,
                expected_acl_revision=kb.acl_revision,
            )
        )


def test_budget_check(test_settings):
    manager = IndexCacheManager(test_settings.model_copy(update={"index_cache_budget_mb": 1}))
    with pytest.raises(Conflict, match="预算"):
        manager._check_budget(3 * 1048576)
    manager._check_budget(1024)  # 预算内通过


def test_tenant_isolation(env):
    kb_id, settings, builder = env
    index_id = upload_and_build(kb_id, settings, builder)
    manager = IndexCacheManager(settings)
    kb = kb_row(kb_id)
    with pytest.raises(Conflict, match="租户"):
        asyncio.run(
            manager.ensure_loaded(
                session_factory=SessionLocal, kb_id=kb_id, tenant_id=kb.tenant_id + 100,
                expected_index_id=index_id, expected_data_revision=kb.data_revision,
                expected_acl_revision=kb.acl_revision,
            )
        )
