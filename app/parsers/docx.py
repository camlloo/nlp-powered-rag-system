"""DOCX 解析：按文档 XML 顺序遍历段落与表格（实现文档 §4.1 红线）。

不能先读全部段落再读全部表格；记录标题样式、段落号、表格号与行号；
DOCX 无稳定页码，引用使用标题路径与段落位置，不伪造页码。
"""

from __future__ import annotations

import re

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

from app.core.config import Settings
from app.core.errors import NeedsReview
from app.parsers.base import check_zip_expansion
from app.schemas.contracts import ParsedDocument, TableInfo, TextBlock

HEADING_STYLE_RE = re.compile(r"^(?:Heading|标题)\s*(\d)", re.IGNORECASE)


def parse_docx(payload: bytes, title: str, settings: Settings) -> ParsedDocument:
    check_zip_expansion(payload, settings)
    try:
        document = Document(_Bytes(payload))
    except Exception as exc:  # noqa: BLE001 任何 python-docx 异常都转为可解释失败
        raise NeedsReview(f"DOCX 解析失败：{exc.__class__.__name__}") from exc

    blocks: list[TextBlock] = []
    block_no = 0
    table_no = 0
    first_h1: str | None = None

    for item in _iter_block_items(document):
        if isinstance(item, Paragraph):
            text = item.text.strip()
            if not text:
                continue
            level = _heading_level(item)
            kind = "heading" if level else "paragraph"
            block_no += 1
            blocks.append(
                TextBlock(
                    block_no=block_no,
                    kind=kind,  # type: ignore[arg-type]
                    text=text,
                    level=level,
                    locator={"type": "docx", "paragraph": block_no},
                )
            )
            if kind == "heading" and level == 1 and first_h1 is None:
                first_h1 = text
        elif isinstance(item, Table):
            table_no += 1
            rows = item.rows
            if not rows:
                continue
            headers = [cell.text.strip() for cell in rows[0].cells]
            for row_no, row in enumerate(rows[1:], start=1):
                cells = [cell.text.strip() for cell in row.cells]
                if not any(cells):
                    continue
                block_no += 1
                blocks.append(
                    TextBlock(
                        block_no=block_no,
                        kind="table_row",
                        text=" | ".join(cells),
                        locator={"type": "docx", "paragraph": block_no, "row": row_no},
                        table=TableInfo(
                            table_no=table_no, row_no=row_no, headers=headers, row=cells
                        ),
                    )
                )

    doc_title = first_h1 or title
    return ParsedDocument(fmt="docx", title=doc_title, blocks=blocks)


def _iter_block_items(document: Document):
    """按 body XML 真实顺序产出段落与表格（不得按类型分批读取）。"""
    body = document.element.body
    for child in body.iterchildren():
        if child.tag == qn("w:p"):
            yield Paragraph(child, document)
        elif child.tag == qn("w:tbl"):
            yield Table(child, document)


def _heading_level(paragraph: Paragraph) -> int | None:
    style = paragraph.style
    if style is None:
        return None
    matched = HEADING_STYLE_RE.match(style.name or "")
    if matched:
        return min(int(matched.group(1)), 6)
    return None


class _Bytes:
    """python-docx 接受文件路径或文件对象，包一层以便从内存读取。"""

    def __init__(self, data: bytes) -> None:
        import io

        self._fp = io.BytesIO(data)

    def __getattr__(self, name: str):  # noqa: ANN401 透传文件接口
        return getattr(self._fp, name)
