"""问答、引用与反馈接口（实现文档 §9.2/§9.3）。

- POST /qa：检索 + 证据生成；业务结果以 status 表达（200），
  知识库状态变化返回 409；
- GET /citations/{id}：不透明 citation_id，访问时重新授权，
  文档已删除则 404（§5.3）；
- POST /feedback：记录有帮助/无帮助及原因。
"""

from fastapi import APIRouter
from sqlalchemy import select

from app.api.deps import PrincipalDep, SessionDep, require_kb_access
from app.core.config import get_settings
from app.core.errors import NotFound
from app.db.models import KbDocument, KbDocumentVersion, QaCitation, QaFeedback, QaRequest
from app.gateways.factory import get_chat_gateway, get_embedding_gateway
from app.generation.service import QaService
from app.indexing.cache import get_index_cache
from app.retrieval.service import RetrievalService
from app.schemas.qa import (
    CitationDetailResponse,
    CitationOut,
    FeedbackCreated,
    FeedbackRequest,
    QaRequestModel,
    QaResponse,
)

router = APIRouter()


@router.post("/qa", response_model=QaResponse)
async def ask(
    payload: QaRequestModel, session: SessionDep, principal: PrincipalDep
) -> QaResponse:
    require_kb_access(kb_id=payload.kb_id, session=session, principal=principal)
    service = QaService(
        retrieval=RetrievalService(
            get_index_cache(), get_embedding_gateway(), get_settings()
        ),
        chat=get_chat_gateway(),
    )
    outcome = await service.ask(
        kb_id=payload.kb_id,
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        question=payload.question,
        history=[item.model_dump() for item in payload.history],
        top_k=payload.top_k,
    )
    return QaResponse(
        request_id=outcome.request_id,
        status=outcome.status,
        answer=outcome.answer,
        index_id=outcome.index_id,
        retrieval_mode=outcome.retrieval_mode,
        citations=[
            CitationOut(
                source_id=c.source_id,
                citation_id=c.citation_id,
                document_title=c.document_title,
                document_version=c.document_version,
                locator=c.locator,
                quote=c.quote,
            )
            for c in outcome.citations
        ],
    )


@router.get("/citations/{citation_id}", response_model=CitationDetailResponse)
def get_citation(
    citation_id: str, session: SessionDep, principal: PrincipalDep
) -> CitationDetailResponse:
    citation = session.execute(
        select(QaCitation).where(QaCitation.citation_id == citation_id)
    ).scalar_one_or_none()
    if citation is None:
        raise NotFound("引用不存在")
    qa_request = session.get(QaRequest, citation.qa_request_id)
    if qa_request is None:
        raise NotFound("引用不存在")
    require_kb_access(kb_id=qa_request.kb_id, session=session, principal=principal)

    doc_state = session.execute(
        select(KbDocument.deleted_at, KbDocument.title, KbDocumentVersion.version_no)
        .join(KbDocumentVersion, KbDocumentVersion.document_id == KbDocument.id)
        .where(KbDocumentVersion.id == citation.doc_version_id)
    ).first()
    if doc_state is None or doc_state[0] is not None:
        raise NotFound("文档已删除")  # 删除后旧引用不再可访问（§14.3 第 6 条）

    return CitationDetailResponse(
        citation_id=citation.citation_id,
        source_id=citation.source_id,
        document_title=citation.document_title,
        document_version=doc_state[2],
        locator=citation.locator,
        quote=citation.quote,
    )


@router.post("/feedback", status_code=201, response_model=FeedbackCreated)
def submit_feedback(
    payload: FeedbackRequest, session: SessionDep, principal: PrincipalDep
) -> FeedbackCreated:
    qa_request = session.execute(
        select(QaRequest).where(QaRequest.request_id == payload.request_id)
    ).scalar_one_or_none()
    if qa_request is None:
        raise NotFound("问答记录不存在")
    require_kb_access(kb_id=qa_request.kb_id, session=session, principal=principal)
    feedback = QaFeedback(
        qa_request_id=qa_request.id,
        user_id=principal.user_id,
        rating=payload.rating,
        reason=payload.reason,
    )
    session.add(feedback)
    session.commit()
    return FeedbackCreated(feedback_id=feedback.id)
