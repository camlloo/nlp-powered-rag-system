"""问答、引用与反馈接口契约（实现文档 §9.2/§9.3）。"""

from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.schemas.retrieval import HistoryMessage


class QaRequestModel(BaseModel):
    # 缺省时检索主空间（去知识库化：问答面向全部可见文档，可见性由检索层过滤）
    kb_id: int | None = None
    # 会话分组：提供则本轮问答归入该会话（路由层校验归属）
    conversation_id: int | None = None
    question: str = Field(
        min_length=1, max_length=get_settings().retrieval_question_max_chars
    )
    top_k: int = Field(default=6, ge=1, le=10)
    history: list[HistoryMessage] = Field(
        default_factory=list, max_length=get_settings().history_max_rounds * 2
    )


class CitationOut(BaseModel):
    source_id: str
    citation_id: str
    document_title: str
    document_version: int
    locator: dict
    quote: str


class QaResponse(BaseModel):
    request_id: str
    # # answered | insufficient_evidence | clarification_required | validation_failed
    status: str
    answer: str | None
    index_id: int | None
    retrieval_mode: str | None
    citations: list[CitationOut]


class CitationDetailResponse(BaseModel):
    citation_id: str
    source_id: str
    document_title: str
    document_version: int
    locator: dict
    quote: str


class FeedbackRequest(BaseModel):
    request_id: str = Field(min_length=1, max_length=64)
    rating: str = Field(pattern="^(helpful|unhelpful)$")
    reason: str | None = Field(default=None, max_length=1000)


class FeedbackCreated(BaseModel):
    feedback_id: int
