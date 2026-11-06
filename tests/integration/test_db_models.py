"""数据模型集成测试：向量读写往返、唯一键与 CHECK 约束。"""

import numpy as np
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.db.models import (
    KbChunk,
    KbCollection,
    KbDocument,
    KbDocumentVersion,
    KbEmbedding,
    KbIndex,
    KbMember,
)


def _make_chain(session):
    settings = get_settings()
    kb = KbCollection(tenant_id=settings.default_tenant_id, name="it-temp")
    session.add(kb)
    session.flush()
    doc = KbDocument(kb_id=kb.id, title="集成测试文档")
    session.add(doc)
    session.flush()
    version = KbDocumentVersion(
        kb_id=kb.id,
        document_id=doc.id,
        version_no=1,
        title="集成测试文档",
        file_sha256=b"\x00" * 32,
        storage_uri="uploads/it",
        parse_status="pending",
    )
    session.add(version)
    session.flush()
    index = KbIndex(
        kb_id=kb.id,
        source_revision=0,
        embedding_model=settings.embedding_model,
        embedding_dim=1024,
        pipeline_version="it",
        config_json={},
        manifest_json={},
    )
    session.add(index)
    session.flush()
    return kb, doc, version, index


def test_vector_roundtrip(db_session):
    kb, doc, version, index = _make_chain(db_session)
    rng = np.random.default_rng(7)
    vector = rng.standard_normal(1024, dtype=np.float32)
    vector /= np.linalg.norm(vector)

    chunk = KbChunk(
        kb_id=kb.id,
        index_id=index.id,
        document_id=doc.id,
        doc_version_id=version.id,
        chunk_no=0,
        title_path="集成测试文档",
        content="正文",
        search_text="正文",
        embedding_input="集成测试文档 正文",
        input_sha256=b"\x01" * 32,
        tokens=["正文"],
        keywords=["正文"],
        source_locator={"type": "docx"},
    )
    db_session.add(chunk)
    db_session.flush()
    db_session.add(KbEmbedding(chunk_id=chunk.id, embedding=vector))
    db_session.flush()

    stored = np.asarray(db_session.get(KbEmbedding, chunk.id).embedding, dtype=np.float32)
    assert stored.shape == (1024,)
    assert np.allclose(stored, vector, atol=1e-5)


def test_chunk_order_unique(db_session):
    kb, doc, version, index = _make_chain(db_session)
    kwargs = {
        "kb_id": kb.id,
        "index_id": index.id,
        "document_id": doc.id,
        "doc_version_id": version.id,
        "title_path": "t",
        "content": "c",
        "search_text": "c",
        "embedding_input": "t c",
        "input_sha256": b"\x01" * 32,
        "tokens": [],
        "keywords": [],
        "source_locator": {},
    }
    db_session.add(KbChunk(chunk_no=0, **kwargs))
    db_session.add(KbChunk(chunk_no=0, **kwargs))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_index_dim_check(db_session):
    kb, *_ = _make_chain(db_session)
    with pytest.raises(IntegrityError):
        db_session.add(
            KbIndex(
                kb_id=kb.id,
                source_revision=0,
                embedding_model="text-embedding-v4",
                embedding_dim=512,
                pipeline_version="it",
                config_json={},
                manifest_json={},
            )
        )
        db_session.flush()
    db_session.rollback()  # 会话从失败中恢复，仍可用于后续查询
    assert db_session.execute(text("SELECT 1")).scalar_one() == 1


def test_member_role_check(db_session):
    kb, *_ = _make_chain(db_session)
    with pytest.raises(IntegrityError):
        db_session.add(KbMember(kb_id=kb.id, user_id=1, role="admin"))
        db_session.flush()
