"""规则切块：标题路径递归 + 目标长度装填 + 完整句重叠（实现文档 §4.5）。

- 按"文档标题 -> 一级标题 -> 二级标题 -> 段落 -> 句子"自上而下划分；
- 首期以展示正文 400 至 700 字符为目标（CHUNK_MAX_CHARS），硬上限 1000；
- 不足 120 字的片段在相同小节内合并；
- 相邻块保留约 80 至 120 字的完整句子重叠，重叠计入长度上限；
- 不能跨不相关章节拼接；列表保留编号与引导句；
- 表格按"表名、表头、当前行"转换为文本，每块包含同一张表的少量行，长表重复表头。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.core.config import Settings
from app.nlp.sentence import split_sentences
from app.schemas.contracts import ParsedDocument, TextBlock

LIST_MARKER_RE = re.compile(r"^\s*(?:[-*+•·]|\d+[.、)]|（?\d+）)\s+")

# 重叠长度约束（实现文档 §4.5：80 至 120 字，"约"值）
OVERLAP_MIN = 80
OVERLAP_MAX = 120


@dataclass
class ChunkPiece:
    title_path: list[str]
    content: str
    locators: list[dict] = field(default_factory=list)
    is_table: bool = False


def chunk_document(parsed: ParsedDocument, settings: Settings) -> list[ChunkPiece]:
    pieces: list[ChunkPiece] = []
    for title_path, blocks in _build_sections(parsed):
        pieces.extend(_chunk_section(title_path, _units_from_blocks(blocks), settings))
    return pieces


def _build_sections(parsed: ParsedDocument) -> list[tuple[list[str], list[TextBlock]]]:
    """按标题层级切分小节；标题路径不含文档标题（文档标题单独入 embedding 前缀）。"""
    sections: list[tuple[list[str], list[TextBlock]]] = []
    stack: list[tuple[int, str]] = []
    current_path: list[str] = []
    current: list[TextBlock] = []

    def close() -> None:
        if current:
            sections.append((list(current_path), current.copy()))

    for block in parsed.blocks:
        if block.kind == "heading" and block.level:
            close()
            current = []
            while stack and stack[-1][0] >= block.level:
                stack.pop()
            stack.append((block.level, block.text))
            current_path = [text for _, text in stack]
        else:
            current.append(block)
    close()
    return sections


def _units_from_blocks(blocks: list[TextBlock]) -> list[tuple[str, dict, bool]]:
    """小节内块转装填单元：连续表格行按组格式化，段落与代码原样。"""
    units: list[tuple[str, dict, bool]] = []
    i = 0
    while i < len(blocks):
        block = blocks[i]
        if block.kind == "table_row" and block.table:
            table_no = block.table.table_no
            rows: list[tuple[int, list[str]]] = []
            first_locator = block.locator
            while (
                i < len(blocks)
                and blocks[i].kind == "table_row"
                and blocks[i].table
                and blocks[i].table.table_no == table_no
            ):
                rows.append((blocks[i].table.row_no, blocks[i].table.row))
                i += 1
            units.append((_format_table(table_no, block.table.headers, rows), first_locator, True))
            continue
        if block.text.strip():
            units.append((block.text, block.locator, False))
        i += 1
    return units


def _format_table(
    table_no: int, headers: list[str], rows: list[tuple[int, list[str]]]
) -> str:
    header = " | ".join(headers) if headers else ""
    lines = [f"表格{table_no} 表头：{header}"]
    for row_no, row in rows:
        lines.append(f"第{row_no}行：{' | '.join(row)}")
    return "\n".join(lines)


def _chunk_section(
    title_path: list[str],
    units: list[tuple[str, dict, bool]],
    settings: Settings,
) -> list[ChunkPiece]:
    target = settings.chunk_max_chars
    hard_max = settings.chunk_hard_max_chars
    min_chars = settings.chunk_min_chars
    overlap = min(max(settings.chunk_overlap_chars, OVERLAP_MIN), OVERLAP_MAX)

    pieces: list[ChunkPiece] = []
    current: list[str] = []  # 每项一条句子（可带 \n 前缀保持段落结构）
    locators: list[dict] = []
    current_len = 0
    current_is_table = False

    def flush() -> None:
        nonlocal current, locators, current_len, current_is_table
        if not current:
            return
        pieces.append(
            ChunkPiece(
                title_path=list(title_path),
                content=_join_parts(current),
                locators=locators.copy(),
                is_table=current_is_table,
            )
        )
        current, locators, current_len, current_is_table = [], [], 0, False

    for unit_text, locator, is_table in units:
        if is_table:
            flush()
            pieces.extend(_table_pieces(title_path, unit_text, locator, target, hard_max))
            continue

        parts = [p for p in unit_text.split("\n") if p.strip()]
        for part in parts:
            for sent_idx, sent in enumerate(_bounded_sentences(part, target)):
                estimate = len(sent) + 1  # 前缀（\n 或 glue 空格）保守按 1 字符计
                if current and current_len + estimate > target:
                    tail = _tail_overlap(current, overlap)
                    flush()
                    if tail and not pieces[-1].is_table:
                        current = list(tail)
                        current_len = len(_join_parts(current))
                        locators = [pieces[-1].locators[0]]
                        if current_len + len(sent) + 1 > hard_max:
                            flush()  # 重叠后仍装不下：放弃重叠，从空块开始
                if current and current_len + len(sent) + 1 > hard_max:
                    flush()
                prefix = ""
                if current:
                    prefix = "\n" if sent_idx == 0 else _glue(current[-1], sent)
                current.append(prefix + sent)
                current_len += len(prefix) + len(sent)
                if not locators or locators[-1] != locator:
                    locators.append(locator)
    flush()

    # 不足 min_chars 的片段在同小节内合并（表格块不参与，硬上限不突破）
    merged: list[ChunkPiece] = []
    for piece in pieces:
        prev = merged[-1] if merged else None
        if (
            prev is not None
            and not piece.is_table
            and not prev.is_table
            and len(piece.content) < min_chars
            and len(prev.content) + len(piece.content) + 1 <= hard_max
        ):
            prev.content = f"{prev.content}\n{piece.content}"
            prev.locators.extend(piece.locators)
            continue
        merged.append(piece)
    return merged


def _table_pieces(
    title_path: list[str], text: str, locator: dict, target: int, hard_max: int
) -> list[ChunkPiece]:
    """表格组装填：整组不超限则单块；超限按行拆分并重复表头。"""
    if len(text) <= hard_max:
        return [
            ChunkPiece(
                title_path=list(title_path), content=text, locators=[locator], is_table=True
            )
        ]
    lines = text.split("\n")
    header, rows = lines[0], lines[1:]
    out: list[ChunkPiece] = []
    buf = [header]
    size = len(header)
    for row in rows:
        if size + len(row) + 1 > target and len(buf) > 1:
            out.append(
                ChunkPiece(
                    title_path=list(title_path),
                    content="\n".join(buf),
                    locators=[locator],
                    is_table=True,
                )
            )
            buf = [header]
            size = len(header)
        buf.append(row)
        size += len(row) + 1
    if buf:
        out.append(
            ChunkPiece(
                title_path=list(title_path),
                content="\n".join(buf),
                locators=[locator],
                is_table=True,
            )
        )
    return out


def _bounded_sentences(text: str, target: int) -> list[str]:
    """分句；超长句按次级边界切分，最终以硬长度兜底。"""
    sentences: list[str] = []
    for sent in split_sentences(text):
        if len(sent) <= target:
            sentences.append(sent)
            continue
        start = 0
        while start < len(sent):
            window = sent[start : start + target]
            if start + target >= len(sent):
                sentences.append(window)
                break
            cut = max(window.rfind(sep) for sep in ("，", "、", "：", ";", ",", " "))
            if cut <= 0:
                cut = target - 1
            sentences.append(sent[start : start + cut + 1])
            start += cut + 1
    return sentences


def _tail_overlap(parts: list[str], overlap: int) -> list[str]:
    """取尾部完整句子作重叠，总量不超过 overlap；单句即超限时放弃重叠。"""
    taken: list[str] = []
    total = 0
    for part in reversed(parts):
        if total + len(part) > overlap:
            break
        taken.insert(0, part)
        total += len(part)
        if total >= OVERLAP_MIN:
            return taken
    return taken


def _glue(prev: str, nxt: str) -> str:
    """中英文拼接修正：前尾与后首均为 ASCII 字母/数字时补空格。"""
    if not prev or not nxt:
        return ""
    if _is_ascii_alnum(prev[-1]) and _is_ascii_alnum(nxt.lstrip()[0] if nxt.strip() else ""):
        return " "
    return ""


def _is_ascii_alnum(ch: str) -> bool:
    return ("a" <= ch <= "z") or ("A" <= ch <= "Z") or ("0" <= ch <= "9")


def _join_parts(parts: list[str]) -> str:
    out = ""
    for part in parts:
        out = part if not out else out + _glue(out, part) + part
    return out
