"""问答、引用与反馈接口契约（实现文档 §9.2/§9.3）。"""

from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.schemas.retrieval import HistoryMessage


class QaRequestModel(BaseModel):
    kb_id: int
    question: str = Field(
        min_length=1, max_length=get_settings().retrieval_question_max_chars
    )
    top_k: int = Field(default=6, ge=1, le=10)
    history: list[HistoryMessage] = Field(
        default_factory=list, max_length=get_settings().history_max_rounds
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
