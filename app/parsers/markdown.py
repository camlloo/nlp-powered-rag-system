"""Markdown 解析：按标题/列表/代码块识别结构，保留代码内容（实现文档 §4.1）。

轻量行解析，不引入额外依赖；标题级别与标题路径信息供切块递归使用。
"""

from __future__ import annotations

import re

from app.core.config import Settings
from app.schemas.contracts import ParsedDocument, TextBlock

HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
LIST_RE = re.compile(r"^\s*(?:[-*+]|\d+[.、)]|（?\d+）)\s+")


def parse_markdown(payload: bytes, title: str, settings: Settings) -> ParsedDocument:
    text = _decode(payload)
    blocks: list[TextBlock] = []
    block_no = 0
    first_h1: str | None = None

    lines = text.splitlines()
    i = 0
    line_no = 0
    while i < len(lines):
        raw = lines[i]
        line_no += 1
        stripped = raw.strip()

        if not stripped:  # 空行
            i += 1
            continue

        in_fence = stripped.startswith("```") or stripped.startswith("~~~")
        if in_fence:
            fence = stripped[:3]
            code_lines = []
            start_line = line_no
            i += 1
            while i < len(lines) and not lines[i].strip().startswith(fence):
                line_no += 1
                code_lines.append(lines[i])
                i += 1
            i += 1  # 跳过闭合围栏
            line_no += 1
            block_no += 1
            blocks.append(
                TextBlock(
                    block_no=block_no,
                    kind="code",
                    text="\n".join(code_lines),
                    locator={"type": "markdown", "line": start_line},
                )
            )
            continue

        heading = HEADING_RE.match(stripped)
        if heading:
            level = len(heading.group(1))
            block_no += 1
            blocks.append(
                TextBlock(
                    block_no=block_no,
                    kind="heading",
                    text=heading.group(2),
                    level=level,
                    locator={"type": "markdown", "line": line_no},
                )
            )
            if level == 1 and first_h1 is None:
                first_h1 = heading.group(2)
            i += 1
            continue

        # 连续普通行或列表行聚合为一段；列表保留编号（实现文档 §4.5）
        group: list[str] = [raw.rstrip()]
        is_list = bool(LIST_RE.match(raw))
        i += 1
        while i < len(lines):
            nxt = lines[i]
            nxt_stripped = nxt.strip()
            if not nxt_stripped:
                break
            if HEADING_RE.match(nxt_stripped) or nxt_stripped.startswith(("```", "~~~")):
                break
            if is_list != bool(LIST_RE.match(nxt)):
                break
            line_no += 1
            group.append(nxt.rstrip())
            i += 1
        block_no += 1
        blocks.append(
            TextBlock(
                block_no=block_no,
                kind="paragraph",
                text="\n".join(group),
                locator={"type": "markdown", "line": line_no - len(group) + 1},
            )
        )

    doc_title = first_h1 or title
    return ParsedDocument(fmt="markdown", title=doc_title, blocks=blocks)


def _decode(payload: bytes) -> str:
    return payload.decode("utf-8", errors="replace")
