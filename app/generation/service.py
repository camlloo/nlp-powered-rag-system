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

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings, get_settings
from app.core.errors import Conflict
from app.db.models import KbCollection, KbDocument, KbDocumentVersion, QaCitation, QaRequest
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
        tenant_id: int,
        user_id: int,
        question: str,
        history: list[dict] | None = None,
        top_k: int = 6,
    ) -> QaOutcome:
        started = time.monotonic()
        request_id = f"qa_{uuid.uuid4().hex}"
        settings = self._settings

        with self._sessions() as session:
            kb = session.get(KbCollection, kb_id)
            if kb is None:
                raise Conflict("知识库不存在")
            ask_time_state = (kb.active_index_id, kb.data_revision, kb.acl_revision)

        result = await self._retrieval.retrieve(
            kb_id=kb_id,
            tenant_id=tenant_id,
            question=question,
            history=history,
            top_k=top_k,
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
            )

        slots = build_slots(result.evidence)
        context = build_context(slots, question, history, settings)
        citations = self._build_citations(context.slots)

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
        )

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

    def _build_citations(self, slots) -> list[CitationInfo]:
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
    ) -> None:
        with self._sessions() as session:
            qa_request = QaRequest(
                request_id=request_id,
                tenant_id=tenant_id,
                user_id=user_id,
                kb_id=kb_id,
                index_id=index_id,
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
            session.commit()
