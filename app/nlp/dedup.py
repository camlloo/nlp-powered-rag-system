"""去重：文件级 SHA-256、块级规范化文本哈希、字符 5-gram Jaccard 近重复（实现文档 §4.4）。

近重复仅生成候选，阈值初始 0.9 须经样本验证；涉及金额、日期、否定词、
版本或例外条件变化的文本不得按近重复直接丢弃。
"""

from __future__ import annotations

import hashlib

from app.nlp.entities import entity_signature

NGRAM_SIZE = 5
NEAR_DUP_THRESHOLD = 0.9

# 否定/条件标记：出现次数不同即视为受保护差异
_NEGATION_WORDS = (
    "不", "未", "无", "禁止", "不得", "不应", "不能",
    "除外", "除非", "至少", "最多", "低于", "超过", "以上", "以下",
)


def file_sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def text_sha256(text: str) -> bytes:
    """块级规范化文本哈希（与 kb_chunk.input_sha256 同为 BYTEA 口径）。"""
    return hashlib.sha256(text.encode("utf-8")).digest()


def char_ngrams(text: str, n: int = NGRAM_SIZE) -> set[str]:
    compact = "".join(text.split())
    if len(compact) < n:
        return {compact} if compact else set()
    return {compact[i : i + n] for i in range(len(compact) - n + 1)}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def five_gram_jaccard(a: str, b: str) -> float:
    return jaccard(char_ngrams(a), char_ngrams(b))


def is_protected_difference(text_a: str, text_b: str) -> bool:
    """两段文本是否存在受保护差异：实体签名不同或否定/条件标记计数不同。"""
    if entity_signature(text_a) != entity_signature(text_b):
        return True
    for word in _NEGATION_WORDS:
        if text_a.count(word) != text_b.count(word):
            return True
    return False


def near_duplicate_pairs(
    items: list[tuple[int, str]], threshold: float = NEAR_DUP_THRESHOLD
) -> list[tuple[int, int]]:
    """块级近重复候选（O(n²)，文档内块数量级下可接受），返回 (i, j) 索引对。

    受保护差异的候选不输出——它们看起来相似但语义不同，必须保留独立来源。
    """
    ngrams = {idx: char_ngrams(text) for idx, text in items}
    texts = dict(items)
    pairs: list[tuple[int, int]] = []
    ordered = [idx for idx, _ in items]
    for pos, i in enumerate(ordered):
        for j in ordered[pos + 1 :]:
            if jaccard(ngrams[i], ngrams[j]) >= threshold:
                if is_protected_difference(texts[i], texts[j]):
                    continue
                pairs.append((i, j))
    return sorted(pairs)
