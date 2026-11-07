"""精确向量召回与 RRF 融合（实现文档 §10.2 原样落实，函数即测试用例）。

- dense_topk：矩阵与查询向量均已 L2 归一化，全排序保证同分结果可复现；
- rrf_merge：融合分值 = Σ w/(60+rank)，rank 从 1 起，仅利用排序，
  不直接相加量纲不同的 BM25 与余弦分值（§7.4）。
"""

from __future__ import annotations

import numpy as np

RRF_CONSTANT = 60


def dense_topk(
    matrix: np.ndarray, chunk_ids: list[int], query_vector: np.ndarray, k: int = 50
) -> list[tuple[int, float]]:
    """全库精确余弦召回 TopK（§7.3：独立于 BM25 的完整召回范围）。"""
    if matrix.shape[0] != len(chunk_ids):
        raise ValueError("chunk alignment mismatch")
    if matrix.ndim != 2 or matrix.shape[1] != query_vector.size:
        raise ValueError("vector space mismatch")
    if k <= 0 or not chunk_ids:
        return []
    scores = matrix @ query_vector
    order = sorted(range(len(chunk_ids)), key=lambda i: (-float(scores[i]), chunk_ids[i]))
    return [(chunk_ids[i], float(scores[i])) for i in order[:k]]


def rrf_merge(
    ranked_lists: list[list[int]],
    weights: list[float] | None = None,
    constant: int = RRF_CONSTANT,
) -> list[tuple[int, float]]:
    """按 RRF 融合多个通道的 chunk_id 排名；输出按 (-分值, chunk_id) 确定性排序。"""
    weights = weights if weights is not None else [1.0] * len(ranked_lists)
    if len(weights) != len(ranked_lists):
        raise ValueError("weight count mismatch")
    scores: dict[int, float] = {}
    for ids, weight in zip(ranked_lists, weights, strict=True):
        seen: set[int] = set()
        for rank, chunk_id in enumerate(ids, start=1):
            if chunk_id in seen:
                raise ValueError("duplicate chunk in ranking")
            seen.add(chunk_id)
            scores[chunk_id] = scores.get(chunk_id, 0.0) + (weight / (constant + rank))
    return sorted(scores.items(), key=lambda x: (-x[1], x[0]))
