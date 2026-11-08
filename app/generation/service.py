"""问答编排：检索 → 门槛 → 上下文 → 生成 → 校验（重生成一次）→ 修订号复核 → 审计。

实现文档 §3.3、§8：

- 无合格证据直接 insufficient_evidence，不调用生成模型（§7.5）；
- 生成失败保留已检索来源，不把错误伪装成答案（§6.2）；
- 输出前复核知识库修订号，变化则 409（§3.3）；
- qa_request / qa_citation / qa_feedback 审计三表记录全量轨迹。
"""

from __future__ import annotations

import logging
import secrets
import time
import uuid
from dataclasses import dataclass, field

from sqlalchemy import func, select, update
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings, get_settings
from app.core.errors import AppError, Conflict, NotFound
from app.db.models import (
    Conversation,
    KbCollection,
    KbDocument,
    KbDocumentVersion,
    QaCitation,
    QaRequest,
)
from app.db.session import SessionLocal
from app.gateways.chat import ChatGateway, TokenUsage
from app.gateways.mock import MockChatGateway
from app.generation.context import build_context, build_slots, first_sentence
from app.generation.prompts import SYSTEM_PROMPT, SYSTEM_PROMPT_VERSION
from app.generation.validator import validate_answer
from app.retrieval.service import RetrievalService

logger = logging.getLogger(__name__)

INSUFFICIENT_ANSWER = "当前知识库中未找到足够依据回答该问题。"


@dataclass
class CitationInfo:
    source_id: str
    citation_id: str  # 不透明标识，GET /citations/{id} 重新授权
    chunk_id: int
    doc_version_id: int
    document_title: str
    document_version: int
    locator: dict
    quote: str


@dataclass
class QaOutcome:
    request_id: str
    # # answered | insufficient_evidence | clarification_required | validation_failed
    status: str
    answer: str | None
    index_id: int | None
    retrieval_mode: str | None
    citations: list[CitationInfo] = field(default_factory=list)
    error_code: str | None = None
    usage: TokenUsage = field(default_factory=TokenUsage)


class QaService:
    def __init__(
        self,
        retrieval: RetrievalService,
        chat: ChatGateway | MockChatGateway,
        settings: Settings | None = None,
        session_factory: sessionmaker = SessionLocal,
    ) -> None:
        self._retrieval = retrieval
        self._chat = chat
        self._settings = settings or get_settings()
        self._sessions = session_factory

    async def ask(
        self,
        *,
        kb_id: int,
        principal,
        question: str,
        history: list[dict] | None = None,
        top_k: int = 6,
        conversation_id: int | None = None,
    ) -> QaOutcome:
        started = time.monotonic()
        request_id = f"qa_{uuid.uuid4().hex}"
        settings = self._settings
        tenant_id = principal.tenant_id
        user_id = principal.user_id

        with self._sessions() as session:
            kb = session.get(KbCollection, kb_id)
            if kb is None or kb.deleted_at is not None or kb.tenant_id != tenant_id:
                raise NotFound("知识库不存在")
            ask_time_state = (kb.active_index_id, kb.data_revision, kb.acl_revision)

        result = await self._retrieval.retrieve(
            kb_id=kb_id,
            tenant_id=tenant_id,
            question=question,
            history=history,
            top_k=top_k,
            principal=principal,
        )
        usage = TokenUsage()

        if result.status == "clarification_required":
            return self._finish(
                request_id=request_id,
                status="clarification_required",
                answer=None,
                result=result,
                citations=[],
                usage=usage,
                started=started,
                kb_id=kb_id,
                tenant_id=tenant_id,
                user_id=user_id,
                question=question,
                top_k=top_k,
                expected_state=ask_time_state,
                conversation_id=conversation_id,
            )
        if result.status == "insufficient_evidence":
            # 证据缺失：直接拒答，不调用生成模型（§7.5）
            return self._finish(
                request_id=request_id,
                status="insufficient_evidence",
                answer=INSUFFICIENT_ANSWER,
                result=result,
                citations=[],
                usage=usage,
                started=started,
                kb_id=kb_id,
                tenant_id=tenant_id,
                user_id=user_id,
                question=question,
                top_k=top_k,
                expected_state=ask_time_state,
                conversation_id=conversation_id,
            )

        slots = build_slots(result.evidence)
        context = build_context(slots, question, history, settings)
        citations = self.build_citations(context.slots)

        answer: str | None = None
        validation_error: str | None = None
        for _attempt in range(2):  # 首次 + 最多重生成一次（§8.3）
            chat_result = await self._chat.generate(
                system_prompt=SYSTEM_PROMPT,
                user_payload=context.user_payload,
                budget_scope_id="qa",
            )
            usage = chat_result.usage
            answer = chat_result.content
            validation = validate_answer(answer, context.valid_source_ids)
            if validation.ok:
                validation_error = None
                break
            validation_error = validation.error_code
            logger.warning(
                "qa=%s 答案校验未通过（%s），最多重生成一次", request_id, validation.error_code
            )
        if validation_error is not None:
            # 展示合法来源供用户查看（§8.3），不输出未校验答案
            return self._finish(
                request_id=request_id,
                status="answer_validation_failed",
                answer=None,
                result=result,
                citations=citations,
                usage=usage,
                started=started,
                kb_id=kb_id,
                tenant_id=tenant_id,
                user_id=user_id,
                question=question,
                top_k=top_k,
                expected_state=ask_time_state,
                conversation_id=conversation_id,
                error_code=validation_error,
            )

        return self._finish(
            request_id=request_id,
            status="answered",
            answer=answer,
            result=result,
            citations=citations,
            usage=usage,
            started=started,
            kb_id=kb_id,
            tenant_id=tenant_id,
            user_id=user_id,
            question=question,
            top_k=top_k,
            expected_state=ask_time_state,
            conversation_id=conversation_id,
        )

    async def stream_ask(
        self,
        *,
        kb_id: int,
        principal,
        question: str,
        history: list[dict] | None = None,
        top_k: int = 6,
        conversation_id: int | None = None,
    ):
        """流式问答：逐个产出 SSE 事件 {"event": str, "data": dict}。

        序列：meta → citations → delta* → done（或 error / notice）。
        检索、证据门槛与修订号复核在流开始前完成；生成阶段逐片转发。
        流式路径不做静默重生成——校验失败以 notice + status 标注
        （§8.3 的"重生成一次"属于完整校验后返回模式，见非流式 ask）。
        """
        started = time.monotonic()
        request_id = f"qa_{uuid.uuid4().hex}"
        tenant_id = principal.tenant_id
        user_id = principal.user_id
        settings = self._settings

        with self._sessions() as session:
            kb = session.get(KbCollection, kb_id)
            if kb is None or kb.deleted_at is not None or kb.tenant_id != tenant_id:
                raise NotFound("知识库不存在")
            expected_state = (kb.active_index_id, kb.data_revision, kb.acl_revision)

        yield {"event": "meta", "data": {"request_id": request_id, "kb_id": kb_id}}

        result = await self._retrieval.retrieve(
            kb_id=kb_id,
            tenant_id=tenant_id,
            question=question,
            history=history,
            top_k=top_k,
            principal=principal,
        )
        usage = TokenUsage()

        if result.status in ("clarification_required", "insufficient_evidence"):
            answer = None if result.status == "clarification_required" else INSUFFICIENT_ANSWER
            self._finish(
                request_id=request_id,
                result=result,
                expected_state=expected_state,
                started=started,
                tenant_id=tenant_id,
                user_id=user_id,
                kb_id=kb_id,
                question=question,
                status=result.status,
                answer=answer,
                error_code=None,
                top_k=top_k,
                usage=usage,
                citations=[],
                conversation_id=conversation_id,
            )
            yield {
                "event": "done",
                "data": {
                    "status": result.status,
                    "answer": answer,
                    "citations": [],
                    "mode": result.mode,
                },
            }
            return

        # 修订号复核（流开始前）：失败则记审计并以 error 事件收场
        with self._sessions() as session:
            kb = session.get(KbCollection, kb_id)
            current = (kb.active_index_id, kb.data_revision, kb.acl_revision) if kb else None
        if current != expected_state:
            self._write_audit(
                request_id=request_id,
                tenant_id=tenant_id,
                user_id=user_id,
                kb_id=kb_id,
                index_id=result.index_id,
                question=question,
                status="stale_index",
                retrieval_mode=result.mode,
                answer=None,
                error_code="index_state_changed",
                latency_ms=int((time.monotonic() - started) * 1000),
                top_k=top_k,
                usage=usage,
                citations=[],
                conversation_id=conversation_id,
            )
            yield {
                "event": "error",
                "data": {"code": "index_state_changed", "message": "知识库状态已变化，请重新提问"},
            }
            return

        slots = build_slots(result.evidence)
        context = build_context(slots, question, history, settings)
        citations = self.build_citations(context.slots)
        yield {
            "event": "citations",
            "data": {
                "citations": [
                    {
                        "source_id": c.source_id,
                        "citation_id": c.citation_id,
                        "chunk_id": c.chunk_id,
                        "doc_version_id": c.doc_version_id,
                        "document_title": c.document_title,
                        "document_version": c.document_version,
                        "locator": c.locator,
                        "quote": c.quote,
                    }
                    for c in citations
                ]
            },
        }

        accumulated: list[str] = []
        try:
            async for piece in self._chat.generate_stream(
                system_prompt=SYSTEM_PROMPT,
                user_payload=context.user_payload,
                budget_scope_id="qa",
            ):
                if piece["delta"]:
                    accumulated.append(piece["delta"])
                    yield {"event": "delta", "data": {"text": piece["delta"]}}
        except AppError as exc:
            self._write_audit(
                request_id=request_id,
                tenant_id=tenant_id,
                user_id=user_id,
                kb_id=kb_id,
                index_id=result.index_id,
                question=question,
                status="generation_failed",
                retrieval_mode=result.mode,
                answer="".join(accumulated) or None,
                error_code=exc.code,
                latency_ms=int((time.monotonic() - started) * 1000),
                top_k=top_k,
                usage=usage,
                citations=citations,
                conversation_id=conversation_id,
            )
            yield {"event": "error", "data": {"code": exc.code, "message": exc.message}}
            return

        answer = "".join(accumulated).strip()
        validation = validate_answer(answer, context.valid_source_ids)
        status = "answered" if validation.ok else "answer_validation_failed"
        if not validation.ok:
            logger.warning(
                "qa=%s 流式答案校验未通过（%s）", request_id, validation.error_code
            )
            yield {
                "event": "notice",
                "data": {
                    "code": validation.error_code,
                    "message": "答案未通过引用校验，以下为可查证的来源。",
                },
            }

        self._finish(
            request_id=request_id,
            result=result,
            expected_state=expected_state,
            started=started,
            tenant_id=tenant_id,
            user_id=user_id,
            kb_id=kb_id,
            question=question,
            status=status,
            answer=answer or None,
            error_code=validation.error_code,
            top_k=top_k,
            usage=TokenUsage(prompt_tokens=0, total_tokens=len(answer)),
            citations=citations,
            conversation_id=conversation_id,
        )
        yield {
            "event": "done",
            "data": {
                "status": status,
                "answer": answer or None,
                "citations": [
                    {
                        "source_id": c.source_id,
                        "citation_id": c.citation_id,
                        "document_title": c.document_title,
                        "document_version": c.document_version,
                        "locator": c.locator,
                        "quote": c.quote,
                    }
                    for c in citations
                ],
                "mode": result.mode,
                "request_id": request_id,
                "latency_ms": int((time.monotonic() - started) * 1000),
            },
        }

    # ------------------------------------------------------------------ 内部

    def _finish(
        self,
        *,
        request_id: str,
        status: str,
        answer: str | None,
        result,
        citations: list[CitationInfo],
        usage: TokenUsage,
        started: float,
        kb_id: int,
        tenant_id: int,
        user_id: int,
        question: str,
        top_k: int,
        expected_state: tuple,
        error_code: str | None = None,
        conversation_id: int | None = None,
    ) -> QaOutcome:
        """输出前复核修订号（§3.3），随后写审计三表。"""
        with self._sessions() as session:
            kb = session.get(KbCollection, kb_id)
            current = (
                (kb.active_index_id, kb.data_revision, kb.acl_revision) if kb else None
            )
            stale = current != expected_state
        if stale:
            self._write_audit(
                request_id=request_id,
                tenant_id=tenant_id,
                user_id=user_id,
                kb_id=kb_id,
                index_id=result.index_id,
                question=question,
                status="stale_index",
                retrieval_mode=result.mode,
                answer=None,
                error_code="index_state_changed",
                latency_ms=int((time.monotonic() - started) * 1000),
                top_k=top_k,
                usage=usage,
                citations=[],
                conversation_id=conversation_id,
            )
            raise Conflict("知识库状态已变化，请重新提问")

        self._write_audit(
            request_id=request_id,
            tenant_id=tenant_id,
            user_id=user_id,
            kb_id=kb_id,
            index_id=result.index_id,
            question=question,
            status=status,
            retrieval_mode=result.mode,
            answer=answer,
            error_code=error_code,
            latency_ms=int((time.monotonic() - started) * 1000),
            top_k=top_k,
            usage=usage,
            citations=citations,
            conversation_id=conversation_id,
        )
        return QaOutcome(
            request_id=request_id,
            status=status,
            answer=answer,
            index_id=result.index_id,
            retrieval_mode=result.mode,
            citations=citations,
            error_code=error_code,
            usage=usage,
        )

    def build_citations(self, slots) -> list[CitationInfo]:
        if not slots:
            return []
        doc_version_ids = {slot.doc_version_id for slot in slots}
        with self._sessions() as session:
            rows = session.execute(
                select(KbDocumentVersion.id, KbDocument.title, KbDocumentVersion.version_no)
                .join(KbDocument, KbDocument.id == KbDocumentVersion.document_id)
                .where(KbDocumentVersion.id.in_(doc_version_ids))
            ).all()
        title_map = {row_id: (title, version_no) for row_id, title, version_no in rows}
        citations: list[CitationInfo] = []
        for slot in slots:
            title, version_no = title_map.get(
                slot.doc_version_id, ("未知文档", 0)
            )
            citations.append(
                CitationInfo(
                    source_id=slot.source_id,
                    citation_id=secrets.token_urlsafe(16),  # 服务端生成的不可预测标识（§9.3）
                    chunk_id=slot.chunk_id,
                    doc_version_id=slot.doc_version_id,
                    document_title=title,
                    document_version=version_no,
                    locator=slot.locator,
                    quote=first_sentence(slot.content),
                )
            )
        return citations

    def _write_audit(
        self,
        *,
        request_id: str,
        tenant_id: int,
        user_id: int,
        kb_id: int,
        index_id: int | None,
        question: str,
        status: str,
        retrieval_mode: str | None,
        answer: str | None,
        error_code: str | None,
        latency_ms: int,
        top_k: int,
        usage: TokenUsage,
        citations: list[CitationInfo],
        conversation_id: int | None = None,
    ) -> None:
        with self._sessions() as session:
            qa_request = QaRequest(
                request_id=request_id,
                tenant_id=tenant_id,
                user_id=user_id,
                kb_id=kb_id,
                index_id=index_id,
                conversation_id=conversation_id,
                question=question,
                answer_status=status,
                retrieval_mode=retrieval_mode,
                answer=answer,
                error_code=error_code,
                latency_ms=latency_ms,
                model_config_json={
                    "chat_model": self._settings.chat_model,
                    "prompt_version": SYSTEM_PROMPT_VERSION,
                    "top_k": top_k,
                    "evidence_budget": self._settings.context_evidence_budget_tokens,
                },
                usage_json={
                    "prompt_tokens": usage.prompt_tokens,
                    "total_tokens": usage.total_tokens,
                },
            )
            session.add(qa_request)
            session.flush()
            for citation in citations:
                session.add(
                    QaCitation(
                        qa_request_id=qa_request.id,
                        source_id=citation.source_id,
                        citation_id=citation.citation_id,
                        chunk_id=citation.chunk_id,
                        doc_version_id=citation.doc_version_id,
                        document_title=citation.document_title,
                        locator=citation.locator,
                        quote=citation.quote,
                    )
                )
            if conversation_id is not None:
                # 会话最近使用时间 = 最后一轮问答（同一事务，任务列表按此排序）
                session.execute(
                    update(Conversation)
                    .where(Conversation.id == conversation_id)
                    .values(updated_at=func.now())
                )
            session.commit()
