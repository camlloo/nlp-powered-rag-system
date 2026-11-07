"""文本型 PDF 解析：逐页抽取、质量打分（实现文档 §4.1）。

按可读文字比例、异常字符比例和页内文本密度打分；阈值由样本校准，
低质量文件转 needs_review，不能直接发布索引。扫描件不进入首期范围。
"""

from __future__ import annotations

import io
import re
import unicodedata

from pypdf import PdfReader

from app.core.config import Settings
from app.core.errors import NeedsReview
from app.nlp.cleaning import fold_pdf_noise
from app.parsers.base import ensure_page_count
from app.schemas.contracts import ParsedDocument, QualityReport, TextBlock

_BULLET_RE = re.compile(r"^\s*(?:[-*+•·]|\d+[.、)])\s+")


def parse_pdf(payload: bytes, title: str, settings: Settings) -> ParsedDocument:
    try:
        reader = PdfReader(io.BytesIO(payload))
        if reader.is_encrypted:
            raise NeedsReview("PDF 已加密，不支持解析")
        ensure_page_count(len(reader.pages), settings)
        page_texts: list[str] = []
        for page in reader.pages:
            page_texts.append(page.extract_text() or "")
    except NeedsReview:
        raise
    except Exception as exc:  # noqa: BLE001 损坏的 PDF 统一转为可解释的 needs_review
        raise NeedsReview(f"PDF 读取失败：{exc.__class__.__name__}") from exc

    # 抽取噪声归一化（点线引导符/连续空白）：打分与切块使用同一份折叠后文本
    page_texts = [fold_pdf_noise(t) for t in page_texts]

    quality = _score_pages(page_texts, settings)
    blocks = [
        TextBlock(
            block_no=page_no + 1,
            kind="paragraph",
            text=_merge_soft_wraps(raw),
            locator={"type": "pdf", "page": page_no + 1, "block": 1},
        )
        for page_no, raw in enumerate(page_texts)
        if raw.strip()
    ]
    return ParsedDocument(
        fmt="pdf",
        title=title,
        blocks=blocks,
        page_count=len(page_texts),
        quality=quality,
    )


def _score_pages(page_texts: list[str], settings: Settings) -> QualityReport:
    total = 0
    readable = 0
    abnormal = 0
    for text in page_texts:
        for ch in text:
            if ch in "\n\t":
                continue
            total += 1
            if _is_abnormal(ch):
                abnormal += 1
            elif not ch.isspace():
                readable += 1
    readable_ratio = readable / total if total else 0.0
    abnormal_ratio = abnormal / total if total else 1.0
    non_empty = [t for t in page_texts if t.strip()]
    min_page_chars = min((len(t.strip()) for t in non_empty), default=0)

    reasons: list[str] = []
    if readable_ratio < settings.pdf_min_readable_ratio:
        reasons.append(
            f"可读字符比例 {readable_ratio:.2f} 低于阈值 {settings.pdf_min_readable_ratio}"
        )
    if abnormal_ratio > settings.pdf_max_abnormal_ratio:
        reasons.append(
            f"异常字符比例 {abnormal_ratio:.2f} 高于阈值 {settings.pdf_max_abnormal_ratio}"
        )
    if min_page_chars < settings.pdf_min_page_chars:
        reasons.append(f"存在文本密度过低的页面（最少 {min_page_chars} 字符）")

    return QualityReport(
        readable_ratio=round(readable_ratio, 4),
        abnormal_char_ratio=round(abnormal_ratio, 4),
        min_page_chars=min_page_chars,
        needs_review=bool(reasons),
        reasons=reasons,
    )


def _is_abnormal(ch: str) -> bool:
    if ch == "\ufffd":
        return True
    code = ord(ch)
    if 0xE000 <= code <= 0xF8FF:  # 私有区
        return True
    return unicodedata.category(ch) == "Cc" and ch not in "\n\t"


def _merge_soft_wraps(raw: str) -> str:
    """PDF 软换行合并：按句末标点与列表标记判定；连字符按断词/型号区分处理（§4.2）。"""
    lines = [line.rstrip() for line in raw.splitlines()]
    merged: list[str] = []
    for line in lines:
        if not line.strip():
            continue
        if not merged:
            merged.append(line)
            continue
        prev = merged[-1]
        if _is_soft_wrap(prev, line):
            stripped_next = line.lstrip()
            if prev.endswith("-") and stripped_next:
                first = stripped_next[0]
                if first.islower():
                    merged[-1] = prev[:-1] + stripped_next  # 断词延续：去连字符
                    continue
                if first.isdigit() or first.isupper():
                    merged[-1] = prev + stripped_next  # 型号/编号：保留连字符
                    continue
            merged[-1] = (
                prev + line
                if (_is_cjk(prev[-1]) or _is_cjk(stripped_next[0]))
                else f"{prev} {line}"
            )
        else:
            merged.append(line)
    return "\n".join(merged)


def _is_soft_wrap(prev: str, line: str) -> bool:
    """上一行不以句末标点/冒号结束，且当前行不是列表项或标题式新行。"""
    if not prev.strip():
        return False
    if _BULLET_RE.match(line):
        return False
    if prev.rstrip().endswith(("。", "！", "？", "；", "：", "!", "?", ";", ":")):
        return False
    return True


def _is_cjk(ch: str) -> bool:
    return "\u4e00" <= ch <= "\u9fff" or "\u3000" <= ch <= "\u303f" or ch in "，。！？；：、（）”"
