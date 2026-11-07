"""消融实验（实现文档 §14.4）。

1) 检索通道消融：lexical / vector / hybrid / hybrid+重排 四种模式在同一快照上
   对同一评测集分别计算 Hit@5 与 MRR@10；
2) 切块策略消融：规则切块（流水线产出）vs 固定字符滑窗，在 BM25 词项通道上
   对比 Hit@5——向量通道受 mock 语义限制，切块对比以词项通道为准。

所有对比共用同一标注 hints；报告区分样本量。
"""

from __future__ import annotations

from app.core.config import Settings
from app.indexing.cache import LoadedIndex
from app.nlp.tokenizer import content_terms
from app.retrieval.bm25 import BM25Index
from app.retrieval.fusion import dense_topk, rrf_merge
from app.retrieval.query import preprocess
from app.retrieval.rerank import rerank
from tests.evaluation.dataset import EvalCase
from tests.evaluation.metrics import is_relevant

RETRIEVAL_MODES = ("lexical", "vector", "hybrid", "hybrid_rerank")


def ensure_bm25(loaded: LoadedIndex, *, k1: float, b: float) -> BM25Index:
    index = loaded.derived.get("bm25")
    if index is None:
        index = BM25Index.build(
            loaded.chunk_ids,
            [loaded.records[cid].tokens for cid in loaded.chunk_ids],
            k1=k1,
            b=b,
        )
        loaded.derived["bm25"] = index
    return index


async def rank_for_mode(
    loaded: LoadedIndex,
    question: str,
    mode: str,
    *,
    embedding,
    settings: Settings,
) -> list[int]:
    """按指定模式返回排序后的 chunk_id 列表（服务链路之外的独立口径）。"""
    plan = preprocess(question)
    bm25 = ensure_bm25(loaded, k1=settings.bm25_k1, b=settings.bm25_b)
    lexical_ids = [cid for cid, _ in bm25.search(plan.terms, settings.channel_top_k)]

    if mode == "lexical":
        return lexical_ids
    query_vector = await embedding.embed_query(plan.normalized, tenant_id=loaded.tenant_id)
    vector_ids = [
        cid
        for cid, _ in dense_topk(
            loaded.matrix, loaded.chunk_ids, query_vector, settings.channel_top_k
        )
    ]
    if mode == "vector":
        return vector_ids
    fused = rrf_merge([lexical_ids, vector_ids])
    if mode == "hybrid":
        return [cid for cid, _ in fused]
    reranked = rerank(
        plan.terms, plan.entities, fused, loaded.records, top_k=settings.rerank_top_k
    )
    return [item.chunk_id for item in reranked]


async def channel_ablation(
    loaded: LoadedIndex,
    cases: list[EvalCase],
    *,
    embedding,
    settings: Settings,
) -> dict[str, dict]:
    """四种检索模式各自的 Hit@5 / MRR@10（仅统计可回答题）。"""
    results: dict[str, dict] = {}
    for mode in RETRIEVAL_MODES:
        hit_values: list[float] = []
        mrr_values: list[float] = []
        for case in cases:
            if not case.answerable:
                continue
            ranked_ids = await rank_for_mode(
                loaded, case.question, mode, embedding=embedding, settings=settings
            )
            contents = [loaded.records[cid].content for cid in ranked_ids[:5]]
            hit_values.append(1.0 if any(is_relevant(c, case.hints) for c in contents) else 0.0)
            ranked10 = [loaded.records[cid].content for cid in ranked_ids[:10]]
            first = next(
                (i for i, c in enumerate(ranked10, start=1) if is_relevant(c, case.hints)), 0
            )
            mrr_values.append(1.0 / first if first else 0.0)
        results[mode] = {
            "n": len(hit_values),
            "hit_at_5": round(sum(hit_values) / len(hit_values), 4) if hit_values else None,
            "mrr_at_10": round(sum(mrr_values) / len(mrr_values), 4) if mrr_values else None,
        }
    return results


def fixed_window_chunks(text: str, size: int = 500, overlap: int = 50) -> list[str]:
    """固定字符滑窗切块（消融对照组；生产链路不使用）。"""
    chunks: list[str] = []
    start = 0
    while start < len(text):
        window = text[start : start + size]
        if window.strip():
            chunks.append(window)
        start += size - overlap
    return chunks


def chunking_ablation(
    rule_chunk_texts: list[str], documents: list[str], cases: list[EvalCase], *, k: int = 5
) -> dict[str, dict]:
    """规则切块 vs 固定滑窗：在 BM25 词项通道上对比 Hit@5。"""
    fixed_texts = [chunk for doc in documents for chunk in fixed_window_chunks(doc)]

    def hit_rate(chunk_texts: list[str]) -> dict:
        index = BM25Index.build(
            list(range(len(chunk_texts))),
            [content_terms(chunk) for chunk in chunk_texts],
        )
        hits = []
        for case in cases:
            if not case.answerable:
                continue
            hit_ids = [cid for cid, _ in index.search(content_terms(case.question), k)]
            contents = [chunk_texts[cid] for cid in hit_ids]
            hits.append(1.0 if any(is_relevant(c, case.hints) for c in contents) else 0.0)
        return {
            "n_chunks": len(chunk_texts),
            "n": len(hits),
            "hit_at_5": round(sum(hits) / len(hits), 4) if hits else None,
        }

    return {"rule_chunking": hit_rate(rule_chunk_texts), "fixed_window": hit_rate(fixed_texts)}
