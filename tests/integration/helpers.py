"""集成测试辅助：知识库创建与按子表到父表顺序清理。"""

import uuid

from sqlalchemy import text

from app.db.models import KbCollection
from app.db.session import SessionLocal


def create_kb(name: str | None = None) -> int:
    with SessionLocal() as session:
        kb = KbCollection(tenant_id=1, name=name or f"it-{uuid.uuid4().hex[:8]}")
        session.add(kb)
        session.commit()
        return kb.id


def cleanup_kb(kb_id: int) -> None:
    with SessionLocal() as session:
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
