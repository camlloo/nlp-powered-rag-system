"""证据选择：MMR 去冗余 + 保守拒答门槛（实现文档 §7.4/§7.5）。

- MMR：score = λ*rel - (1-λ)*max_cos(候选, 已选)，λ 初值 0.75，
  相似度直接用已归一化的向量矩阵行内积；
- 数量目标 4-6 块（与 top_k 取小），但 MMR 得分非正的候选不再入选——
  不为凑够数量加入无关证据；
- 拒答门槛是占位保守策略：仅在双路无命中，或顶块余弦低于占位下限且
  查询词零覆盖时拒答；正式阈值须由 M8 开发集联合校准，不设拍脑袋的
  统一可信度数值。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass
class EvidenceGate:
    status: str  # "ok" | "insufficient_evidence"
    metrics: dict = field(default_factory=dict)


def select_evidence(
    reranked: list[tuple[int, float]],
    matrix: np.ndarray,
    chunk_ids: list[int],
    *,
    max_blocks: int = 6,
    mmr_lambda: float = 0.75,
) -> list[int]:
    """从重排结果中选证据块，返回按选择顺序排列的 chunk_id 列表。"""
    if not reranked or max_blocks <= 0:
        return []
    row_of = {cid: pos for pos, cid in enumerate(chunk_ids)}
    top_score = reranked[0][1]
    remaining = list(reranked)
    selected_rows: list[int] = []
    selected: list[int] = []

    while remaining and len(selected) < max_blocks:
        best_item = None
        best_score = -float("inf")
        for chunk_id, score in remaining:
            row = row_of.get(chunk_id)
            if row is None:
                continue
            rel = (score / top_score) if top_score > 0 else 0.0
            if selected_rows:
                sim = float(np.max(matrix[selected_rows] @ matrix[row]))
            else:
                sim = 0.0
            mmr = mmr_lambda * rel - (1 - mmr_lambda) * sim
            if mmr > best_score:
                best_item, best_score = (chunk_id, row), mmr
        if best_item is None:
            break
        if selected and best_score <= 0:
            break  # 剩余候选全是冗余或不相关：宁缺毋滥
        chunk_id, row = best_item
        selected.append(chunk_id)
        selected_rows.append(row)
        remaining = [(cid, s) for cid, s in remaining if cid != chunk_id]
    return selected


def evaluate_gate(
    *,
    has_candidates: bool,
    top_cosine: float | None,
    keyword_coverage: float,
    cosine_floor: float,
) -> EvidenceGate:
    """占位保守拒答判定；metrics 一并返回供 /retrieval/debug 与评测观测。"""
    metrics = {
        "has_candidates": has_candidates,
        "top_cosine": top_cosine,
        "keyword_coverage": round(keyword_coverage, 4),
        "cosine_floor": cosine_floor,
    }
    if not has_candidates:
        return EvidenceGate(status="insufficient_evidence", metrics=metrics)
    if top_cosine is not None and top_cosine < cosine_floor and keyword_coverage == 0:
        return EvidenceGate(status="insufficient_evidence", metrics=metrics)
    return EvidenceGate(status="ok", metrics=metrics)


def keyword_coverage(query_terms: list[str], evidence_texts: list[str]) -> float:
    """查询词项在证据检索文本中的覆盖比例（0-1）。"""
    if not query_terms:
        return 0.0
    haystack = "\n".join(evidence_texts)
    matched = sum(1 for term in set(query_terms) if term in haystack)
    return matched / len(set(query_terms))
