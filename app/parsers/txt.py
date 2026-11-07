"""TXT 解析：UTF-8 优先，失败后按可配置编码回退并记录最终编码（实现文档 §4.1）。"""

from __future__ import annotations

from app.core.config import Settings
from app.core.errors import NeedsReview
from app.schemas.contracts import ParsedDocument, TextBlock


def parse_txt(payload: bytes, title: str, settings: Settings) -> ParsedDocument:
    text, encoding = _decode(payload, settings.fallback_encoding)
    blocks: list[TextBlock] = []
    block_no = 0
    line_cursor = 1
    for para in _paragraphs(text):
        if not para.strip():
            line_cursor += para.count("\n") + 1
            continue
        block_no += 1
        blocks.append(
            TextBlock(
                block_no=block_no,
                kind="paragraph",
                text=para.strip("\n"),
                locator={"type": "txt", "line": line_cursor},
            )
        )
        line_cursor += para.count("\n") + 1
    return ParsedDocument(fmt="txt", title=title, blocks=blocks, encoding=encoding)


def _decode(payload: bytes, fallback: str) -> tuple[str, str]:
    try:
        return payload.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        return payload.decode(fallback), fallback
    except (UnicodeDecodeError, LookupError) as exc:
        raise NeedsReview(f"无法按 UTF-8 或 {fallback} 解码文本") from exc


def _paragraphs(text: str) -> list[str]:
    """按空行分段，保留段内换行。"""
    return text.split("\n\n")
