"""M1 数据层验收脚本：PG 版本、pgvector 扩展版本、vector(1024) 读写往返、约束校验。

全程使用一个事务并在结束时回滚，不在数据库留下任何数据。
"""

import sys

import numpy as np
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.core.config import REQUIRED_EMBEDDING_DIM, get_settings
from app.db.models import (
    KbChunk,
    KbCollection,
    KbDocument,
    KbDocumentVersion,
    KbEmbedding,
    KbIndex,
)
from app.db.session import SessionLocal


def main() -> int:
    settings = get_settings()
    failures: list[str] = []

    with SessionLocal() as session:
        pg_version = session.execute(text("SELECT version()")).scalar_one()
        ext_version = session.execute(
            text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        ).scalar_one_or_none()
        print(f"PostgreSQL: {pg_version}")
        print(f"pgvector:   {ext_version}")
        if ext_version is None:
            failures.append("pgvector 扩展未安装")

        # 构造最小数据链路并验证向量往返（事务回滚，不落库）
        kb = KbCollection(tenant_id=settings.default_tenant_id, name="verify-temp")
        session.add(kb)
        session.flush()
        doc = KbDocument(kb_id=kb.id, title="验证文档")
        session.add(doc)
        session.flush()
        version = KbDocumentVersion(
            kb_id=kb.id,
            document_id=doc.id,
            version_no=1,
            title="验证文档",
            file_sha256=b"\x00" * 32,
            storage_uri="uploads/verify",
            parse_status="pending",
        )
        session.add(version)
        session.flush()
        index = KbIndex(
            kb_id=kb.id,
            source_revision=kb.data_revision,
            embedding_model=settings.embedding_model,
            embedding_dim=REQUIRED_EMBEDDING_DIM,
            pipeline_version="verify",
            config_json={},
            manifest_json={},
        )
        session.add(index)
        session.flush()

        rng = np.random.default_rng(42)
        vector = rng.standard_normal(REQUIRED_EMBEDDING_DIM, dtype=np.float32)
        vector /= np.linalg.norm(vector)

        chunk = KbChunk(
            kb_id=kb.id,
            index_id=index.id,
            document_id=doc.id,
            doc_version_id=version.id,
            chunk_no=0,
            title_path="验证文档",
            content="正文内容",
            search_text="正文内容",
            embedding_input="验证文档 正文内容",
            input_sha256=b"\x01" * 32,
            tokens=["正文", "内容"],
            keywords=["内容"],
            source_locator={"type": "docx", "paragraph": 1},
        )
        session.add(chunk)
        session.flush()
        session.add(KbEmbedding(chunk_id=chunk.id, embedding=vector))
        session.flush()

        stored = session.execute(
            select(KbEmbedding.embedding).where(KbEmbedding.chunk_id == chunk.id)
        ).scalar_one()
        arr = np.asarray(stored, dtype=np.float32)
        if arr.shape != (REQUIRED_EMBEDDING_DIM,):
            failures.append(f"向量维度不符：{arr.shape}")
        elif not np.allclose(arr, vector, atol=1e-5):
            failures.append(f"向量往返数值不一致，最大误差 {np.max(np.abs(arr - vector)):.2e}")
        else:
            print(
                f"向量读写往返 OK（{REQUIRED_EMBEDDING_DIM} 维，"
                f"最大误差 {np.max(np.abs(arr - vector)):.2e}）"
            )

        # 约束校验：embedding_dim != 1024 必须被 CHECK 拒绝
        try:
            session.add(
                KbIndex(
                    kb_id=kb.id,
                    source_revision=0,
                    embedding_model=settings.embedding_model,
                    embedding_dim=512,
                    pipeline_version="verify",
                    config_json={},
                    manifest_json={},
                )
            )
            session.flush()
        except IntegrityError:
            session.rollback()
            print("维度 CHECK 约束 OK（embedding_dim=512 被拒绝）")
        else:
            failures.append("embedding_dim=512 未被 CHECK 约束拒绝")

        session.rollback()

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1
    print("verify_db: 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
