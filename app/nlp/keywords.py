"""关键词抽取：对已生成的 tokens 计算 TF-IDF（实现文档 §4.4）。

- 不使用 jieba.analyse 组件（默认分词会重新启用 HMM）；
- 每块 5 至 8 个关键词；
- 领域 IDF 首期从当前文档集合统计，M4 起可注入已发布知识库的版本化 IDF；
- IDF 口径与 BM25 一致：log(1 + (N - df + 0.5) / (df + 0.5))。
"""

from __future__ import annotations

import math
from collections import Counter

MIN_KEYWORDS = 5
MAX_KEYWORDS = 8


def corpus_idf(tokens_lists: list[list[str]]) -> dict[str, float]:
    """从文档集合统计 IDF（评测期固定实现，不混用不同口径）。"""
    n_docs = len(tokens_lists)
    df: Counter[str] = Counter()
    for tokens in tokens_lists:
        df.update(set(tokens))
    return {
        term: math.log(1 + (n_docs - freq + 0.5) / (freq + 0.5))
        for term, freq in df.items()
    }


def extract_keywords(tokens_lists: list[list[str]]) -> list[list[str]]:
    """对每个 chunk 的 tokens 抽取关键词，输出与输入一一对应。"""
    idf = corpus_idf(tokens_lists)
    return [top_keywords(tokens, idf) for tokens in tokens_lists]


def top_keywords(tokens: list[str], idf: dict[str, float]) -> list[str]:
    if not tokens:
        return []
    tf = Counter(tokens)
    total = len(tokens)
    scored = [
        (count / total * idf.get(term, math.log(1 + 0.5)), term)
        for term, count in tf.items()
    ]
    # 确定性排序：分数降序，同分按词项字典序
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    limit = min(MAX_KEYWORDS, max(MIN_KEYWORDS, 0))
    unique = len({t for _, t in scored})
    limit = min(MAX_KEYWORDS, unique) if unique >= MIN_KEYWORDS else unique
    return [term for _, term in scored[:limit]]
