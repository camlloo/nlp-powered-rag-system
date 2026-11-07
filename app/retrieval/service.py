"""检索编排：预处理 → 双路独立召回 → RRF 融合 → 规则重排 → MMR 证据选择（实现文档 §7）。

一致性约定：
- 每次请求先读知识库行（active_index_id + 修订号），经缓存管理器装载并复核；
- 向量通道独立于 BM25 全库精确召回（§7.3），查询向量超时降级 lexical_only（§6.2）；
- 无合格证据返回 insufficient_evidence，由上层拒答，不调用生成模型（§7.5）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.core.config import Settings, get_settings
from app.core.errors import NotFound
from app.db.models import KbCollection
from app.db.session import SessionLocal
from app.gateways.embedding import EmbeddingGateway
from app.gateways.errors import EmbeddingUnavailable
from app.gateways.mock import MockEmbeddingGateway
from app.indexing.cache import IndexCacheManager
from app.retrieval import bm25 as bm25_module
from app.retrieval.evidence import evaluate_gate, keyword_coverage, select_evidence
from app.retrieval.fusion import dense_topk, rrf_merge
from app.retrieval.query import preprocess
from app.retrieval.rerank import rerank

logger = logging.getLogger(__name__)


@dataclass
class ChannelHit:
    chunk_id: int
    score: float
    cosine: float | None = None  # 仅向量通道填写


@dataclass
class EvidenceBlock:
    chunk_id: int
    doc_version_id: int
    title_path: str
    content: str
    source_locator: dict
    rrf_score: float
    rerank_score: float


@dataclass
class RetrievalResult:
    status: str  # ok | insufficient_evidence | clarification_required
    mode: str  # hybrid | lexical_only
    kb_id: int
    index_id: int | None
    query_original: str
    query_expanded: str
    terms: list[str]
    entities: list[tuple[str, str]]
    lexical: list[ChannelHit] = field(default_factory=list)
    vector: list[ChannelHit] = field(default_factory=list)
    fused: list[ChannelHit] = field(default_factory=list)
    reranked: list[ChannelHit] = field(default_factory=list)
    evidence: list[EvidenceBlock] = field(default_factory=list)
    gate: dict = field(default_factory=dict)


class RetrievalService:
    def __init__(
        self,
        cache: IndexCacheManager,
        embedding: EmbeddingGateway | MockEmbeddingGateway,
        settings: Settings | None = None,
    ) -> None:
        self._cache = cache
        self._embedding = embedding
        self._settings = settings or get_settings()

    async def retrieve(
        self,
        *,
        session_factory=SessionLocal,
        kb_id: int,
        tenant_id: int,
        question: str,
        history: list[dict] | None = None,
        top_k: int = 6,
        principal=None,
    ) -> RetrievalResult:
        settings = self._settings
        plan = preprocess(question, history)
        base = {
            "kb_id": kb_id,
            "index_id": None,
            "query_original": question,
            "query_expanded": plan.normalized,
            "terms": plan.terms,
            "entities": plan.entities,
        }
        if plan.clarification_required:
            return RetrievalResult(
                status="clarification_required", mode="hybrid", **base
            )

        with session_factory() as session:
            kb = session.get(KbCollection, kb_id)
            if kb is None or kb.deleted_at is not None:
                raise NotFound("知识库不存在")
            if kb.active_index_id is None:
                gate = evaluate_gate(
                    has_candidates=False,
                    top_cosine=None,
                    keyword_coverage=0.0,
                    cosine_floor=settings.evidence_gate_cosine_floor,
                )
                return RetrievalResult(
                    status="insufficient_evidence", mode="hybrid", gate=gate.metrics, **base
                )
            expected = (kb.active_index_id, kb.data_revision, kb.acl_revision)

        loaded = await self._cache.ensure_loaded(
            session_factory=session_factory,
            kb_id=kb_id,
            tenant_id=tenant_id,
            expected_index_id=expected[0],
            expected_data_revision=expected[1],
            expected_acl_revision=expected[2],
        )
        base["index_id"] = loaded.index_id
        if not loaded.chunk_ids:
            gate = evaluate_gate(
                has_candidates=False,
                top_cosine=None,
                keyword_coverage=0.0,
                cosine_floor=settings.evidence_gate_cosine_floor,
            )
            return RetrievalResult(
                status="insufficient_evidence", mode="hybrid", gate=gate.metrics, **base
            )

        # 检索级可见性过滤：自己的 ∨ 公开 ∨ 组织标签匹配（管理员旁路）
        allowed_ids: set[int] | None = None
        if principal is not None and not principal.can_see_all():
            allowed_ids = {
                cid
                for cid, record in loaded.records.items()
                if record.uploader_user_id == principal.user_id
                or record.visibility == "public"
                or (record.org_tag_id is not None and record.org_tag_id in principal.org_tag_ids)
            }
            if not allowed_ids:
                gate = evaluate_gate(
                    has_candidates=False,
                    top_cosine=None,
                    keyword_coverage=0.0,
                    cosine_floor=settings.evidence_gate_cosine_floor,
                )
                return RetrievalResult(
                    status="insufficient_evidence",
                    mode="hybrid",
                    gate=gate.metrics,
                    **base,
                )

        # 词项通道：BM25（派生结构挂载在快照缓存上，随快照换入换出）
        index_bm25 = loaded.derived.get("bm25")
        if index_bm25 is None:
            index_bm25 = bm25_module.BM25Index.build(
                loaded.chunk_ids,
                [loaded.records[cid].tokens for cid in loaded.chunk_ids],
                k1=settings.bm25_k1,
                b=settings.bm25_b,
            )
            loaded.derived["bm25"] = index_bm25
        lexical = index_bm25.search(
            plan.terms, settings.channel_top_k, allowed_ids=allowed_ids
        )

        # 向量通道：独立全库精确召回；查询向量超时降级（§6.2 lexical_only）
        mode = "hybrid"
        vector_hits: list[tuple[int, float]] = []
        try:
            query_vector = await self._embedding.embed_query(plan.normalized, tenant_id=tenant_id)
            vector_hits = dense_topk(
                loaded.matrix,
                loaded.chunk_ids,
                query_vector,
                settings.channel_top_k,
                allowed_ids=allowed_ids,
            )
        except EmbeddingUnavailable:
            mode = "lexical_only"
            logger.warning("kb=%s 查询向量不可用，降级 BM25 词项召回", kb_id)

        fused = rrf_merge([[cid for cid, _ in lexical], [cid for cid, _ in vector_hits]])
        reranked = rerank(
            plan.terms, plan.entities, fused, loaded.records, top_k=settings.rerank_top_k
        )
        evidence_ids = select_evidence(
            [(item.chunk_id, item.rerank_score) for item in reranked],
            loaded.matrix,
            loaded.chunk_ids,
            max_blocks=min(max(1, top_k), settings.evidence_max_blocks),
            mmr_lambda=settings.mmr_lambda,
        )

        top_cosine = vector_hits[0][1] if vector_hits else None
        coverage = (
            keyword_coverage(plan.terms, [loaded.records[cid].search_text for cid in evidence_ids])
            if evidence_ids
            else 0.0
        )
        gate = evaluate_gate(
            has_candidates=bool(fused),
            top_cosine=top_cosine,
            keyword_coverage=coverage,
            cosine_floor=settings.evidence_gate_cosine_floor,
        )

        rrf_scores = dict(fused)
        rerank_scores = {item.chunk_id: item.rerank_score for item in reranked}
        evidence = []
        for chunk_id in evidence_ids:
            record = loaded.records[chunk_id]
            evidence.append(
                EvidenceBlock(
                    chunk_id=chunk_id,
                    doc_version_id=record.doc_version_id,
                    title_path=record.title_path,
                    content=record.content,
                    source_locator=record.source_locator,
                    rrf_score=rrf_scores.get(chunk_id, 0.0),
                    rerank_score=rerank_scores.get(chunk_id, 0.0),
                )
            )

        return RetrievalResult(
            status="ok" if gate.status == "ok" else "insufficient_evidence",
            mode=mode,
            kb_id=kb_id,
            index_id=loaded.index_id,
            query_original=question,
            query_expanded=plan.normalized,
            terms=plan.terms,
            entities=plan.entities,
            lexical=[ChannelHit(cid, score) for cid, score in lexical],
            vector=[ChannelHit(cid, score, cosine=score) for cid, score in vector_hits],
            fused=[ChannelHit(cid, score) for cid, score in fused],
            reranked=[ChannelHit(item.chunk_id, item.rerank_score) for item in reranked],
            evidence=evidence,
            gate=gate.metrics,
        )
