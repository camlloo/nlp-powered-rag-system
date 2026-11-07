"""集成测试辅助：知识库创建与按子表到父表顺序清理。"""

import uuid
from pathlib import Path

from sqlalchemy import delete, select, text, update

from app.core.config import get_settings
from app.db.models import (
    IngestJob,
    KbChunk,
    KbCollection,
    KbDocument,
    KbDocumentVersion,
    KbEmbedding,
)
from app.db.session import SessionLocal


def cleanup_uploaded_documents(uploader_user_ids: list[int]) -> int:
    """按上传者硬删文档及其块/向量/版本/任务与物理上传文件（测试残留清理）。

    主空间是共享设施：删行递增 data_revision 使缓存失效，并同步删除
    data/uploads 下的原始文件，避免每次 pytest 都堆积孤儿上传残留。
    """
    if not uploader_user_ids:
        return 0
    with SessionLocal() as session:
        doc_ids = session.scalars(
            select(KbDocument.id).where(KbDocument.uploader_user_id.in_(uploader_user_ids))
        ).all()
        if not doc_ids:
            return 0
        kb_ids = set(
            session.scalars(
                select(KbCollection.id)
                .join(KbDocument, KbDocument.kb_id == KbCollection.id)
                .where(KbDocument.id.in_(doc_ids))
            ).all()
        )
        version_ids = session.scalars(
            select(KbDocumentVersion.id).where(KbDocumentVersion.document_id.in_(doc_ids))
        ).all()
        storage_uris = session.scalars(
            select(KbDocumentVersion.storage_uri).where(
                KbDocumentVersion.id.in_(version_ids)
            )
        ).all() if version_ids else []
        chunk_ids = session.scalars(
            select(KbChunk.id).where(KbChunk.document_id.in_(doc_ids))
        ).all()
        if chunk_ids:
            session.execute(delete(KbEmbedding).where(KbEmbedding.chunk_id.in_(chunk_ids)))
        session.execute(delete(KbChunk).where(KbChunk.document_id.in_(doc_ids)))
        if version_ids:
            session.execute(delete(IngestJob).where(IngestJob.doc_version_id.in_(version_ids)))
        session.execute(
            delete(KbDocumentVersion).where(KbDocumentVersion.document_id.in_(doc_ids))
        )
        session.execute(delete(KbDocument).where(KbDocument.id.in_(doc_ids)))
        if kb_ids:
            session.execute(
                update(KbCollection)
                .where(KbCollection.id.in_(kb_ids))
                .values(data_revision=KbCollection.data_revision + 1)
            )
        session.commit()
    # 提交成功后再删物理文件：失败不影响数据库一致性（残留可由一次性脚本清理）
    root = Path(get_settings().storage_dir)
    for uri in storage_uris:
        f = root / uri
        if f.is_file():
            f.unlink()
    return len(doc_ids)


def create_kb(name: str | None = None) -> int:
    with SessionLocal() as session:
        kb = KbCollection(tenant_id=1, name=name or f"it-{uuid.uuid4().hex[:8]}")
        session.add(kb)
        session.commit()
        return kb.id


def cleanup_kb(kb_id: int) -> None:
    with SessionLocal() as session:
        # 该库全部版本的物理上传文件在提交后删除
        uris = session.scalars(
            select(KbDocumentVersion.storage_uri).where(KbDocumentVersion.kb_id == kb_id)
        ).all()
        session.execute(
            text(
                "DELETE FROM kb_embedding WHERE chunk_id IN "
                "(SELECT id FROM kb_chunk WHERE kb_id = :k)"
            ),
            {"k": kb_id},
        )
        tables = (
            "kb_chunk",
            "kb_index",
            "ingest_job",
            "kb_document_version",
            "kb_document",
            "kb_member",
        )
        for table in tables:
            session.execute(text(f"DELETE FROM {table} WHERE kb_id = :k"), {"k": kb_id})
        session.execute(text("DELETE FROM kb_collection WHERE id = :k"), {"k": kb_id})
        session.commit()
    root = Path(get_settings().storage_dir)
    for uri in uris:
        f = root / uri
        if f.is_file():
            f.unlink()


def build_pdf(page_texts: list[str]) -> bytes:
    """构造最小合法 PDF（仅 ASCII 文本，pypdf 可完整解析）。"""
    chunks: list[bytes] = [b"%PDF-1.4\n"]
    offsets: dict[int, int] = {}

    def add(obj_id: int, content: bytes) -> None:
        offsets[obj_id] = sum(len(c) for c in chunks)
        chunks.append(f"{obj_id} 0 obj\n".encode() + content + b"endobj\n")

    n = len(page_texts)
    page_ids = [3 + 2 * i for i in range(n)]
    content_ids = [4 + 2 * i for i in range(n)]
    font_id = 3 + 2 * n

    add(1, b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    add(2, f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode())
    for i, text_content in enumerate(page_texts):
        add(
            page_ids[i],
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Contents {content_ids[i]} 0 R "
                f"/Resources << /Font << /F1 {font_id} 0 R >> >> >>"
            ).encode(),
        )
        stream = f"BT /F1 12 Tf 72 720 Td ({text_content}) Tj ET".encode("latin-1")
        add(
            content_ids[i],
            f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream",
        )
    add(font_id, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    xref_start = sum(len(c) for c in chunks)
    size = font_id + 1
    lines = [f"xref\n0 {size}\n0000000000 65535 f \n"]
    for obj_id in range(1, size):
        lines.append(f"{offsets[obj_id]:010d} 00000 n \n")
    chunks.append("".join(lines).encode())
    chunks.append(
        f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref_start}\n%%EOF\n".encode()
    )
    return b"".join(chunks)
