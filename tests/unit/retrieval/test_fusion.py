"""融合层单测：dense_topk 与 rrf_merge 按实现文档 §10.2 示例验证。"""

import numpy as np
import pytest

from app.retrieval.fusion import dense_topk, rrf_merge


def _matrix(vectors: list[list[float]]) -> np.ndarray:
    arr = np.asarray(vectors, dtype=np.float32)
    return arr / np.linalg.norm(arr, axis=1, keepdims=True)


def test_dense_topk_orders_by_similarity():
    matrix = _matrix([[1, 0], [0, 1], [0.9, 0.1]])
    query = np.asarray([0.9, 0.1], dtype=np.float32)
    query = query / np.linalg.norm(query)
    hits = dense_topk(matrix, [11, 22, 33], query, k=2)
    assert [chunk_id for chunk_id, _ in hits] == [33, 11]
    assert all(0.0 <= score <= 1.0001 for _, score in hits)


def test_dense_topk_tie_breaks_by_chunk_id():
    matrix = _matrix([[1, 0], [1, 0]])
    query = np.asarray([1, 0], dtype=np.float32)
    hits = dense_topk(matrix, [7, 3], query, k=2)
    assert [chunk_id for chunk_id, _ in hits] == [3, 7]  # 同分按 chunk_id 升序


def test_dense_topk_alignment_mismatch():
    matrix = _matrix([[1, 0], [0, 1]])
    query = np.asarray([1, 0], dtype=np.float32)
    with pytest.raises(ValueError, match="alignment"):
        dense_topk(matrix, [1], query)
    with pytest.raises(ValueError, match="space"):
        dense_topk(matrix, [1, 2], np.asarray([1, 0, 0], dtype=np.float32))
    assert dense_topk(matrix, [1, 2], query, k=0) == []


def test_rrf_merge_known_scores():
    # A=[10,20] B=[20,30]：10 在 A rank1 → 1/61；20 在 A rank2 + B rank1 → 1/62+1/61；30 → 1/62
    fused = rrf_merge([[10, 20], [20, 30]])
    scores = dict(fused)
    assert [chunk_id for chunk_id, _ in fused] == [20, 10, 30]
    assert scores[20] == pytest.approx(1 / 61 + 1 / 62)
    assert scores[10] == pytest.approx(1 / 61)
    assert scores[30] == pytest.approx(1 / 62)


def test_rrf_merge_tie_break_and_miss_channel():
    # 未命中通道贡献为 0；同分按 chunk_id 排序
    fused = rrf_merge([[5], [9]])
    assert [chunk_id for chunk_id, _ in fused] == [5, 9]


def test_rrf_merge_guards():
    with pytest.raises(ValueError, match="duplicate"):
        rrf_merge([[1, 1]])
    with pytest.raises(ValueError, match="weight count"):
        rrf_merge([[1], [2]], weights=[1.0])
