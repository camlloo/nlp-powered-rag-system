"""PDF 解析单测：最小合法 PDF 构造、质量打分、软换行与连字符合并。"""

import pytest

from app.core.config import Settings
from app.core.errors import NeedsReview, PayloadTooLarge
from app.parsers.base import ensure_page_count
from app.parsers.pdf import _merge_soft_wraps, parse_pdf


def _build_pdf(page_texts: list[str]) -> bytes:
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
    for i, text in enumerate(page_texts):
        add(
            page_ids[i],
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Contents {content_ids[i]} 0 R "
                f"/Resources << /Font << /F1 {font_id} 0 R >> >> >>"
            ).encode(),
        )
        stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
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


def test_pdf_parse_text():
    parsed = parse_pdf(
        _build_pdf(["Employee shall submit expense within 30 days. Receipts required."]),
        "policy",
        Settings(),
    )
    assert parsed.page_count == 1
    assert "30 days" in parsed.blocks[0].text
    assert parsed.quality.needs_review is False
    assert parsed.blocks[0].locator["page"] == 1


def test_pdf_garbled_needs_review():
    with pytest.raises(NeedsReview, match="PDF"):
        parse_pdf(b"%PDF-1.4\n" + b"\x00\x01" * 64, "broken", Settings())


def test_pdf_page_limit():
    with pytest.raises(PayloadTooLarge, match="页数"):
        ensure_page_count(301, Settings(max_pdf_pages=300))


def test_pdf_low_density_needs_review():
    parsed = parse_pdf(_build_pdf(["ok"]), "thin", Settings())
    assert parsed.quality.needs_review is True
    assert any("密度" in reason for reason in parsed.quality.reasons)


def test_merge_soft_wraps_sentence_punct():
    assert _merge_soft_wraps("出门检查门窗。\n第二天再确认。") == "出门检查门窗。\n第二天再确认。"
    assert _merge_soft_wraps("这是第一行没有句号\n第二行继续。") == "这是第一行没有句号第二行继续。"


def test_merge_soft_wraps_hyphen():
    assert _merge_soft_wraps("inter-\nnational service") == "international service"
    assert _merge_soft_wraps("型号 XY-\n100 参数") == "型号 XY-100 参数"


def test_merge_soft_wraps_keeps_list_items():
    merged = _merge_soft_wraps("前置说明没有句号\n1. 第一项\n2. 第二项")
    assert "\n1. 第一项" in merged
    assert "\n2. 第二项" in merged
