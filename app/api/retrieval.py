"""检索调试接口（实现文档 §9.2：POST /retrieval/debug）。

输出各通道召回、融合与重排明细，仅认证用户可访问；
后期 RBAC 启用时在此收紧为 editor 及以上角色。
"""

from fastapi import APIRouter

from app.api.deps import PrincipalDep, SessionDep, require_kb_access
from app.core.config import get_settings
from app.db.spaces import get_or_create_main_space
from app.gateways.factory import get_embedding_gateway
from app.indexing.cache import get_index_cache
from app.retrieval.service import RetrievalService
from app.schemas.retrieval import (
    ChannelHitOut,
    EvidenceOut,
    RetrievalDebugRequest,
    RetrievalDebugResponse,
)

router = APIRouter()


@router.post("/retrieval/debug", response_model=RetrievalDebugResponse)
async def retrieval_debug(
    payload: RetrievalDebugRequest, session: SessionDep, principal: PrincipalDep
) -> RetrievalDebugResponse:
    kb_id = payload.kb_id
    if kb_id is None:
        kb_id = get_or_create_main_space(session).id
    else:
        require_kb_access(kb_id=kb_id, session=session, principal=principal)
    service = RetrievalService(
        get_index_cache(), get_embedding_gateway(), get_settings()
    )
    result = await service.retrieve(
        kb_id=kb_id,
        tenant_id=principal.tenant_id,
        question=payload.question,
        history=[item.model_dump() for item in payload.history],
        top_k=payload.top_k,
        principal=principal,
    )
    return RetrievalDebugResponse(
        kb_id=result.kb_id,
        index_id=result.index_id,
        status=result.status,
        mode=result.mode,
        query_original=result.query_original,
        query_expanded=result.query_expanded,
        terms=result.terms,
        entities=[list(pair) for pair in result.entities],
        lexical=[ChannelHitOut(chunk_id=h.chunk_id, score=h.score) for h in result.lexical],
        vector=[
            ChannelHitOut(chunk_id=h.chunk_id, score=h.score, cosine=h.cosine)
            for h in result.vector
        ],
        fused=[ChannelHitOut(chunk_id=h.chunk_id, score=h.score) for h in result.fused],
        reranked=[ChannelHitOut(chunk_id=h.chunk_id, score=h.score) for h in result.reranked],
        evidence=[
            EvidenceOut(
                chunk_id=e.chunk_id,
                doc_version_id=e.doc_version_id,
                title_path=e.title_path,
                content=e.content,
                source_locator=e.source_locator,
                rrf_score=e.rrf_score,
                rerank_score=e.rerank_score,
            )
            for e in result.evidence
        ],
        gate=result.gate,
    )
