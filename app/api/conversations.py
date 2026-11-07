"""任务/会话接口：历史对话列表、详情还原与删除（仅本人会话）。

对话数据不单独存储——qa_request 审计已保存每轮问答，会话只是分组：
详情接口把审计行还原为前端消息序列（引用带 document_version 供卷宗展示）。
"""

from __future__ import annotations

from fastapi import APIRouter, Query
from sqlalchemy import func, select

from app.api.deps import PrincipalDep, SessionDep
from app.core.errors import NotFound
from app.db.models import Conversation, KbDocumentVersion, QaCitation, QaRequest
from app.schemas.conversation import (
    ConversationCreateRequest,
    ConversationDetailResponse,
    ConversationItem,
    ConversationListResponse,
    RestoredCitation,
    RestoredMessage,
)

router = APIRouter()


def _conversation_item(conv: Conversation) -> ConversationItem:
    return ConversationItem(
        conversation_id=conv.id,
        title=conv.title,
        created_at=conv.created_at,
        updated_at=conv.updated_at,
    )


def get_own_conversation(session, conversation_id: int, principal) -> Conversation:
    """归属校验：不存在 / 非本租户 / 非本人 → 统一 404（不泄露存在性）。"""
    conv = session.get(Conversation, conversation_id)
    if conv is None or conv.tenant_id != principal.tenant_id or conv.user_id != principal.user_id:
        raise NotFound("会话不存在")
    return conv


@router.get("/conversations", response_model=ConversationListResponse)
def list_conversations(
    session: SessionDep,
    principal: PrincipalDep,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> ConversationListResponse:
    """当前用户的会话列表，按最近更新排序（侧边栏任务列表）。"""
    filters = [
        Conversation.tenant_id == principal.tenant_id,
        Conversation.user_id == principal.user_id,
    ]
    total = session.scalar(
        select(func.count()).select_from(Conversation).where(*filters)
    )
    rows = session.scalars(
        select(Conversation)
        .where(*filters)
        .order_by(Conversation.updated_at.desc(), Conversation.id.desc())
        .offset(offset)
        .limit(limit)
    ).all()
    return ConversationListResponse(
        total=total,
        items=[_conversation_item(conv) for conv in rows],
    )


@router.post("/conversations", status_code=201, response_model=ConversationItem)
def create_conversation(
    payload: ConversationCreateRequest, session: SessionDep, principal: PrincipalDep
) -> ConversationItem:
    """新建任务（首个问题即标题，由前端传入）；空会话在首问前不落审计。"""
    conv = Conversation(
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        title=payload.title[:120],
    )
    session.add(conv)
    session.commit()
    session.refresh(conv)
    return _conversation_item(conv)


@router.get("/conversations/{conversation_id}", response_model=ConversationDetailResponse)
def get_conversation(
    conversation_id: int, session: SessionDep, principal: PrincipalDep
) -> ConversationDetailResponse:
    """会话详情：审计行还原为消息序列（提问 + 回答 + 引用）。"""
    conv = get_own_conversation(session, conversation_id, principal)
    qa_rows = session.scalars(
        select(QaRequest)
        .where(QaRequest.conversation_id == conv.id)
        .order_by(QaRequest.id)
    ).all()
    request_ids = [row.id for row in qa_rows]
    citation_map: dict[int, list[RestoredCitation]] = {rid: [] for rid in request_ids}
    if request_ids:
        citation_rows = session.execute(
            select(QaCitation, KbDocumentVersion.version_no)
            .outerjoin(KbDocumentVersion, KbDocumentVersion.id == QaCitation.doc_version_id)
            .where(QaCitation.qa_request_id.in_(request_ids))
            .order_by(QaCitation.id)
        ).all()
        for citation, version_no in citation_rows:
            citation_map[citation.qa_request_id].append(
                RestoredCitation(
                    source_id=citation.source_id,
                    citation_id=citation.citation_id,
                    document_title=citation.document_title,
                    document_version=version_no,
                    locator=dict(citation.locator),
                    quote=citation.quote,
                )
            )

    messages: list[RestoredMessage] = []
    for row in qa_rows:
        messages.append(
            RestoredMessage(
                role="user", content=row.question, created_at=row.created_at
            )
        )
        messages.append(
            RestoredMessage(
                role="assistant",
                status=row.answer_status,
                answer=row.answer,
                request_id=row.request_id,
                error_code=row.error_code,
                citations=citation_map.get(row.id, []),
                created_at=row.created_at,
            )
        )
    return ConversationDetailResponse(
        conversation_id=conv.id,
        title=conv.title,
        updated_at=conv.updated_at,
        messages=messages,
    )


@router.delete("/conversations/{conversation_id}", status_code=204)
def delete_conversation(
    conversation_id: int, session: SessionDep, principal: PrincipalDep
) -> None:
    """删除会话（审计行保留：conversation_id 成为悬空历史值，符合审计表设计）。"""
    conv = get_own_conversation(session, conversation_id, principal)
    session.delete(conv)
    session.commit()
