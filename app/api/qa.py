"""问答、引用与反馈接口（实现文档 §9.2/§9.3）。

- POST /qa：检索 + 证据生成；业务结果以 status 表达（200），
  知识库状态变化返回 409；
- GET /citations/{id}：不透明 citation_id，访问时重新授权，
  文档已删除则 404（§5.3）；
- POST /feedback：记录有帮助/无帮助及原因。
"""

import json

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from app.api.conversations import get_own_conversation
from app.api.deps import PrincipalDep, SessionDep, require_kb_access
from app.core.config import get_settings
from app.core.errors import AppError, NotFound
from app.db.models import KbDocument, KbDocumentVersion, QaCitation, QaFeedback, QaRequest
from app.db.spaces import get_or_create_main_space
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


def _build_qa_service() -> QaService:
    return QaService(
        retrieval=RetrievalService(
            get_index_cache(), get_embedding_gateway(), get_settings()
        ),
        chat=get_chat_gateway(),
    )


def _sse(event: str, data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False)
    return "event: " + event + "\ndata: " + payload + "\n\n"


def _resolve_kb_id(payload: QaRequestModel, session: SessionDep, principal: PrincipalDep) -> int:
    """缺省检索主空间（任何登录用户可问，可见性由检索层过滤）；
    显式传 kb_id 时保持 KB 成员校验（内部机制与测试兼容）。"""
    if payload.kb_id is not None:
        require_kb_access(kb_id=payload.kb_id, session=session, principal=principal)
        return payload.kb_id
    return get_or_create_main_space(session).id


def _validate_conversation(
    payload: QaRequestModel, session: SessionDep, principal: PrincipalDep
) -> int | None:
    """会话归属校验：非本人/不存在 → 404（在流开始前以常规 HTTP 返回）。"""
    if payload.conversation_id is None:
        return None
    get_own_conversation(session, payload.conversation_id, principal)
    return payload.conversation_id


@router.post("/qa/stream")
async def ask_stream(
    payload: QaRequestModel, session: SessionDep, principal: PrincipalDep
) -> StreamingResponse:
    """SSE 流式问答：meta → citations → delta* → done（或 notice / error）。

    检索与修订号复核在流开始前完成，其间的业务错误（404/409 等）
    仍以常规 HTTP 状态返回。
    """
    kb_id = _resolve_kb_id(payload, session, principal)
    conversation_id = _validate_conversation(payload, session, principal)
    service = _build_qa_service()

    async def gen():
        try:
            async for event in service.stream_ask(
                kb_id=kb_id,
                principal=principal,
                question=payload.question,
                history=[item.model_dump() for item in payload.history],
                top_k=payload.top_k,
                conversation_id=conversation_id,
            ):
                yield _sse(event["event"], event["data"])
        except AppError as exc:
            yield _sse("error", exc.to_body())
        except Exception as exc:  # noqa: BLE001  流中异常以 error 事件收尾
            yield _sse(
                "error",
                {"code": "internal_error", "message": exc.__class__.__name__},
            )

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/qa", response_model=QaResponse)
async def ask(
    payload: QaRequestModel, session: SessionDep, principal: PrincipalDep
) -> QaResponse:
    kb_id = _resolve_kb_id(payload, session, principal)
    conversation_id = _validate_conversation(payload, session, principal)
    service = QaService(
        retrieval=RetrievalService(
            get_index_cache(), get_embedding_gateway(), get_settings()
        ),
        chat=get_chat_gateway(),
    )
    outcome = await service.ask(
        kb_id=kb_id,
        principal=principal,
        question=payload.question,
        history=[item.model_dump() for item in payload.history],
        top_k=payload.top_k,
        conversation_id=conversation_id,
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
    # 去知识库化：KB 级成员校验取消，文档级可见性检查（下方）承担授权

    doc_state = session.execute(
        select(KbDocument, KbDocumentVersion.version_no)
        .join(KbDocumentVersion, KbDocumentVersion.document_id == KbDocument.id)
        .where(KbDocumentVersion.id == citation.doc_version_id)
    ).first()
    if doc_state is None or doc_state[0].deleted_at is not None:
        raise NotFound("文档已删除")  # 删除后旧引用不再可访问（§14.3 第 6 条）
    document, version_no = doc_state
    if (
        not principal.can_see_all()
        and document.uploader_user_id != principal.user_id
        and document.visibility != "public"
        and document.org_tag_id not in principal.org_tag_ids
    ):
        raise NotFound("引用不存在")

    return CitationDetailResponse(
        citation_id=citation.citation_id,
        source_id=citation.source_id,
        document_title=citation.document_title,
        document_version=version_no,
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
    feedback = QaFeedback(
        qa_request_id=qa_request.id,
        user_id=principal.user_id,
        rating=payload.rating,
        reason=payload.reason,
    )
    session.add(feedback)
    session.commit()
    return FeedbackCreated(feedback_id=feedback.id)
