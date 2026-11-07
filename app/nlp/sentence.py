"""规则分句：中文按"。！？；"及段落边界，保护小数、IP、版本号、URL、编号等（实现文档 §4.3）。

引号闭合后再确认句末边界；每条保护规则配样本测试防止误切。
"""

from __future__ import annotations

import re

SENT_PUNCT = "。！？；!?;."

# 保护模式：命中部分用占位符替换，切分后还原
_PROTECT_RES = [
    re.compile(r"https?://[^\s，。；！？、]+", re.IGNORECASE),
    re.compile(r"\d{1,3}(?:\.\d{1,3}){3}"),  # IPv4
    re.compile(r"\d+(?:\.\d+)+"),  # 小数与版本号 30.5 / 2.1.3
    re.compile(r"(?:[eE]\.[gG]\.|[iI]\.[eE]\.|etc\.|vs\.|No\.|Mr\.|Dr\.)"),
    re.compile(r"[A-Za-z]{1,10}-\d+(?:[A-Za-z-]*\d)*"),  # 型号 XY-100
    re.compile(r"[¥￥]\s*\d[\d,，]*(?:\.\d+)?"),  # 金额符号前缀
]

_PLACEHOLDER = "\x00{}\x01"
_PLACEHOLDER_RE = re.compile("\x00(\\d+)\x01")

# 成对引号：句末边界只在引号闭合后确认
_QUOTE_PAIRS = {"“": "”", "«": "»", "「": "」", "『": "』", '"': '"', "'": "'"}


def split_sentences(text: str) -> list[str]:
    """分句并保留句尾符号；不修改任何被保护的模式。"""
    if not text or not text.strip():
        return []

    protected: list[str] = []

    def _protect(match: re.Match[str]) -> str:
        protected.append(match.group(0))
        return _PLACEHOLDER.format(len(protected) - 1)

    masked = text
    for pattern in _PROTECT_RES:
        masked = pattern.sub(_protect, masked)

    sentences: list[str] = []
    buffer: list[str] = []
    quote_stack: list[str] = []
    for ch in masked:
        buffer.append(ch)
        if quote_stack and ch == quote_stack[-1]:
            quote_stack.pop()
        elif ch in _QUOTE_PAIRS:
            quote_stack.append(ch)
        if ch in SENT_PUNCT and not quote_stack:
            sentences.append("".join(buffer))
            buffer = []
    tail = "".join(buffer)
    if tail:
        if tail.strip() or not sentences:
            sentences.append(tail)
        else:
            sentences[-1] += tail  # 仅剩空白时并入上一句，保留原样

    restored = []
    for sent in sentences:
        sent = _PLACEHOLDER_RE.sub(lambda m: protected[int(m.group(1))], sent)
        if sent.strip():
            restored.append(sent)
    return restored
