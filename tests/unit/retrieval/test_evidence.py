"""证据选择单测：MMR 去冗余、数量边界、保守拒答门槛（实现文档 §7.4/§7.5）。"""

import numpy as np
import pytest

from app.retrieval.evidence import evaluate_gate, keyword_coverage, select_evidence


def _matrix(rows: list[list[float]]) -> np.ndarray:
    arr = np.asarray(rows, dtype=np.float32)
    return arr / np.linalg.norm(arr, axis=1, keepdims=True)


def test_mmr_prefers_diverse_directions():
    # 0/1 同向，2/3 同向：λ=0.75 时应从两组方向各选一块，而非连选同向
    matrix = _matrix([[1, 0], [0.999, 0.044], [0, 1], [0.044, 0.999]])
    chunk_ids = [10, 11, 20, 21]
    reranked = [(cid, 1.0) for cid in chunk_ids]  # 相关度相同
    selected = select_evidence(reranked, matrix, chunk_ids, max_blocks=2, mmr_lambda=0.75)
    assert len(selected) == 2
    rows = [chunk_ids.index(cid) for cid in selected]
    # 两个被选块的向量不能近乎平行
    cos = float(matrix[rows[0]] @ matrix[rows[1]])
    assert cos < 0.5


def test_mmr_respects_max_blocks():
    matrix = _matrix([[1, 0], [0, 1], [1, 1]])
    reranked = [(i, 1.0) for i in (1, 2, 3)]
    selected = select_evidence(reranked, matrix, [1, 2, 3], max_blocks=6)
    assert len(selected) == 3  # 候选不足上限时全取（都非冗余）


def test_mmr_stops_on_zero_margin():
    matrix = _matrix([[1, 0]])
    reranked = [(1, 1.0), (2, 0.0), (3, 0.0)]  # 2/3 无向量行且零相关
    selected = select_evidence(reranked, matrix, [1], max_blocks=6)
    assert selected == [1]  # 宁缺毋滥，不为凑数量加入无关证据


def test_gate_insufficient_when_no_candidates():
    gate = evaluate_gate(
        has_candidates=False, top_cosine=None, keyword_coverage=0.0, cosine_floor=0.2
    )
    assert gate.status == "insufficient_evidence"


def test_gate_conservative_placeholder():
    # 顶块余弦尚可 → 不拒答（保守，避免误拒）
    ok = evaluate_gate(
        has_candidates=True, top_cosine=0.55, keyword_coverage=0.0, cosine_floor=0.2
    )
    assert ok.status == "ok"
    # 余弦极低且零词项覆盖 → 拒答
    bad = evaluate_gate(
        has_candidates=True, top_cosine=0.05, keyword_coverage=0.0, cosine_floor=0.2
    )
    assert bad.status == "insufficient_evidence"
    # 余弦低但词项有覆盖 → 不拒答（双通道互补）
    lexical = evaluate_gate(
        has_candidates=True, top_cosine=0.05, keyword_coverage=0.5, cosine_floor=0.2
    )
    assert lexical.status == "ok"


def test_keyword_coverage():
    coverage = keyword_coverage(["报销", "时限", "流程"], ["报销应在 30 日内申请，时限以制度为准"])
    assert coverage == pytest.approx(2 / 3)
    assert keyword_coverage([], ["任意"]) == 0.0
