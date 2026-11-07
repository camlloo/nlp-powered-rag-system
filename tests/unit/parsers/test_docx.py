"""DOCX 解析单测：XML 顺序遍历（段落与表格交错）、标题样式、zip 防护。"""

import io

import pytest
from docx import Document

from app.core.config import Settings
from app.core.errors import PayloadTooLarge
from app.parsers.docx import parse_docx


def _build_docx() -> bytes:
    document = Document()
    document.add_heading("第三章 费用报销", level=1)
    document.add_paragraph("员工应在出差结束后 30 日内申请。")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "项目"
    table.cell(0, 1).text = "限额"
    table.cell(1, 0).text = "交通费"
    table.cell(1, 1).text = "500元"
    document.add_paragraph("交通费凭有效票据报销。")
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def test_docx_preserves_xml_order():
    parsed = parse_docx(_build_docx(), "费用制度", Settings())
    kinds = [b.kind for b in parsed.blocks]
    # 关键回归：表格出现在两个段落之间，顺序与文档 XML 一致
    assert kinds == ["heading", "paragraph", "table_row", "paragraph"]


def test_docx_title_from_first_heading():
    parsed = parse_docx(_build_docx(), "费用制度", Settings())
    assert parsed.title == "第三章 费用报销"
    assert parsed.blocks[0].level == 1


def test_docx_table_info():
    parsed = parse_docx(_build_docx(), "费用制度", Settings())
    table_block = parsed.blocks[2]
    assert table_block.table.table_no == 1
    assert table_block.table.headers == ["项目", "限额"]
    assert table_block.table.row == ["交通费", "500元"]
    assert table_block.locator == {"type": "docx", "paragraph": 3, "row": 1}


def test_docx_empty_paragraphs_skipped():
    document = Document()
    document.add_paragraph("")
    document.add_paragraph("有效内容")
    buf = io.BytesIO()
    document.save(buf)
    parsed = parse_docx(buf.getvalue(), "t", Settings())
    assert [b.text for b in parsed.blocks] == ["有效内容"]


def test_docx_zip_guard():
    with pytest.raises(PayloadTooLarge, match="解压"):
        parse_docx(_build_docx(), "t", Settings(docx_zip_guard_bytes=100))
