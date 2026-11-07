"""BM25 词项召回（实现文档 §7.2）。

- 统计范围为该知识库完整快照的 tokens（数据来源 PostgreSQL，缓存可重建）；
- k1=1.5、b=0.75；IDF 固定为 log(1+(N-df+0.5)/(df+0.5))，
  评测期固定本实现，不与其它库的 IDF 口径混用；
- 按确定性次序取 TopK，同分按 chunk_id 排序。
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field


@dataclass
class BM25Index:
    chunk_ids: list[int] = field(default_factory=list)
    idf: dict[str, float] = field(default_factory=dict)
    # term -> [(doc_pos, tf)]
    postings: dict[str, list[tuple[int, float]]] = field(default_factory=dict)
    doc_len: list[int] = field(default_factory=list)
    avgdl: float = 0.0
    k1: float = 1.5
    b: float = 0.75

    @classmethod
    def build(
        cls,
        chunk_ids: list[int],
        token_lists: list[list[str]],
        *,
        k1: float = 1.5,
        b: float = 0.75,
    ) -> BM25Index:
        if len(chunk_ids) != len(token_lists):
            raise ValueError("chunk/token count mismatch")
        n = len(chunk_ids)
        df: Counter[str] = Counter()
        postings: dict[str, list[tuple[int, float]]] = defaultdict(list)
        doc_len: list[int] = []
        for pos, tokens in enumerate(token_lists):
            doc_len.append(len(tokens))
            for term, tf in Counter(tokens).items():
                df[term] += 1
                postings[term].append((pos, float(tf)))
        idf = {
            term: math.log(1 + (n - freq + 0.5) / (freq + 0.5))
            for term, freq in df.items()
        }
        avgdl = (sum(doc_len) / n) if n else 0.0
        return cls(
            chunk_ids=list(chunk_ids),
            idf=dict(idf),
            postings=dict(postings),
            doc_len=doc_len,
            avgdl=avgdl,
            k1=k1,
            b=b,
        )

    def search(self, query_terms: list[str], k: int = 50) -> list[tuple[int, float]]:
        """返回 (chunk_id, bm25_score)，按 (-分值, chunk_id) 确定性排序。"""
        if k <= 0 or not self.chunk_ids or not query_terms:
            return []
        scores: defaultdict[int, float] = defaultdict(float)
        for term in set(query_terms):
            idf = self.idf.get(term)
            if idf is None:
                continue
            for pos, tf in self.postings[term]:
                dl_ratio = self.doc_len[pos] / self.avgdl if self.avgdl > 0 else 0.0
                denom = tf + self.k1 * (1 - self.b + self.b * dl_ratio)
                scores[pos] += idf * tf * (self.k1 + 1) / denom
        order = sorted(scores.items(), key=lambda x: (-x[1], self.chunk_ids[x[0]]))
        return [(self.chunk_ids[pos], score) for pos, score in order[:k]]
