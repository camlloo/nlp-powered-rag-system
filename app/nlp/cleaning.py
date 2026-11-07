"""文本清洗：展示文本与检索文本双表示（实现文档 §4.2）。

- 展示文本（content）：尽量保留标点、换行、编号和数值；
- 检索文本（search_text）：NFKC、空白归一化、英文大小写归一化；
  金额、单位、日期、版本号、产品型号、否定词和比较关系必须保留。
- 页眉页脚按规则剔除：至少 3 页且在 60% 以上页面相同位置出现的相似短行。
"""

from __future__ import annotations

import re
import unicodedata

from app.schemas.contracts import ParsedDocument

_SPACE_RE = re.compile(r"[ \t\f\v]+")
_BLANK_RE = re.compile(r"\n{3,}")

# 点线引导符：一个或多个「空白+句点」组合（目录页 " .  .  . " 等）。
# 句点前无空白则不匹配，因此省略号 "..." 与编号 "1." "1.2.3" 不受影响。
_DOT_LEADER_RE = re.compile(r"(?:[ \t]+\.)+[ \t]*")

# 页眉页脚判定：短行（≤40 字符）、去空白后比较
_HEADER_LINE_MAX = 40


def fold_pdf_noise(text: str) -> str:
    """PDF 抽取噪声归一化：折叠点线引导符与连续空白（质量打分与切块前调用）。

    目录页的 " .  .  . " 点线由空格与句点交替构成：空格计入字符总数但不计入
    可读字符，会把可读比例拉低造成 needs_review 误判；残留点号也会进入切块
    污染词项与向量。此处统一折叠为单个空格，换行结构保留。
    """
    t = _DOT_LEADER_RE.sub(" ", text)
    return _SPACE_RE.sub(" ", t)


def normalize_search_text(text: str) -> str:
    """规范化检索文本：与 query 侧使用同一函数，保证词项口径一致。"""
    t = unicodedata.normalize("NFKC", text)
    t = t.replace("\ufeff", "").replace("\u200b", "").replace("\u3000", " ")
    t = _SPACE_RE.sub(" ", t)
    t = re.sub(r" ?\n ?", "\n", t)
    t = _BLANK_RE.sub("\n\n", t)
    # 英文大小写归一化：仅小写化 ASCII 字母，保留数字与 CJK
    t = "".join(ch.lower() if "a" <= ch <= "z" else ch for ch in t)
    return t.strip()


def clean_display_text(text: str) -> str:
    """展示文本清洗：仅去除零宽与控制字符，保留标点、换行、编号与数值。"""
    kept = []
    for ch in text:
        if ch in ("\n", "\t"):
            kept.append(ch)
            continue
        if unicodedata.category(ch) in ("Cc", "Cf"):
            continue
        kept.append(ch)
    return "".join(kept).strip()


def remove_repeated_page_lines(parsed: ParsedDocument) -> list[str]:
    """剔除跨页页眉页脚，返回被移除的行（处理日志）。

    仅对 PDF 生效：至少 3 页且在 60% 以上页面的相同位置（首行/末行）出现
    的相似短行才标为候选；不得仅因句子重复就删除正文中的重复行。
    """
    if parsed.fmt != "pdf" or not parsed.blocks:
        return []

    page_count = len(parsed.blocks)
    if page_count < 3:
        return []

    position_lines: dict[str, dict[str, int]] = {"first": {}, "last": {}}
    for block in parsed.blocks:
        lines = [ln.strip() for ln in block.text.splitlines() if ln.strip()]
        if not lines:
            continue
        for position, line in (("first", lines[0]), ("last", lines[-1])):
            if len(line) <= _HEADER_LINE_MAX:
                key = _normalize_line(line)
                position_lines[position][key] = position_lines[position].get(key, 0) + 1

    threshold = 0.6
    candidates: set[tuple[str, str]] = set()
    for position, counter in position_lines.items():
        for key, count in counter.items():
            if page_count >= 3 and count / page_count >= threshold:
                candidates.add((position, key))

    if not candidates:
        return []

    removed: list[str] = []
    for block in parsed.blocks:
        lines = [ln for ln in block.text.splitlines() if ln.strip()]
        if not lines:
            continue
        kept: list[str] = []
        usable = [i for i, ln in enumerate(lines) if ln.strip()]
        for idx, line in enumerate(lines):
            if not line.strip():
                continue
            position = "first" if idx == usable[0] else ("last" if idx == usable[-1] else "")
            if position and (position, _normalize_line(line)) in candidates:
                removed.append(line.strip())
                continue
            kept.append(line)
        if kept:
            block.text = "\n".join(kept)
        else:
            block.text = ""
    return removed


def _normalize_line(line: str) -> str:
    return unicodedata.normalize("NFKC", line).replace(" ", "").lower()
