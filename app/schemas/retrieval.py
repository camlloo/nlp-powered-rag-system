"""检索调试接口的请求响应模型（实现文档 §9.2）。

question 限长 2000 字、历史最多 HISTORY_MAX_ROUNDS 轮、top_k 1-10，
超限 422，不由后端静默截断（§9.2）。
"""

from pydantic import BaseModel, Field

from app.core.config import get_settings


class HistoryMessage(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str = Field(min_length=1, max_length=2000)


class RetrievalDebugRequest(BaseModel):
    kb_id: int
    question: str = Field(
        min_length=1, max_length=get_settings().retrieval_question_max_chars
    )
    top_k: int = Field(default=6, ge=1, le=10)
    history: list[HistoryMessage] = Field(
        default_factory=list, max_length=get_settings().history_max_rounds
    )


class ChannelHitOut(BaseModel):
    chunk_id: int
    score: float
    cosine: float | None = None


class EvidenceOut(BaseModel):
    chunk_id: int
    doc_version_id: int
    title_path: str
    content: str
    source_locator: dict
    rrf_score: float
    rerank_score: float


class RetrievalDebugResponse(BaseModel):
    kb_id: int
    index_id: int | None
    status: str
    mode: str
    query_original: str
    query_expanded: str
    terms: list[str]
    entities: list[list[str]]
    lexical: list[ChannelHitOut]
    vector: list[ChannelHitOut]
    fused: list[ChannelHitOut]
    reranked: list[ChannelHitOut]
    evidence: list[EvidenceOut]
    gate: dict
