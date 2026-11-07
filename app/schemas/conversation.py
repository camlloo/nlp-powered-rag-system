"""任务/会话接口的请求响应模型（前端任务列表与历史还原）。"""

from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints


class ConversationCreateRequest(BaseModel):
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]


class ConversationItem(BaseModel):
    conversation_id: int
    title: str
    created_at: object | None = None
    updated_at: object | None = None


class ConversationListResponse(BaseModel):
    total: int
    items: list[ConversationItem]


class RestoredCitation(BaseModel):
    source_id: str
    citation_id: str
    document_title: str
    document_version: int | None = None  # 读取时 join kb_document_version 得到
    locator: dict
    quote: str


class RestoredMessage(BaseModel):
    role: str  # user | assistant
    content: str | None = None  # user 的提问
    status: str | None = None  # assistant 的 answer_status
    answer: str | None = None  # 含 [S1] 标记，前端渲染为引注章
    request_id: str | None = None
    error_code: str | None = None
    citations: list[RestoredCitation] = Field(default_factory=list)
    created_at: object | None = None


class ConversationDetailResponse(BaseModel):
    conversation_id: int
    title: str
    updated_at: object | None = None
    messages: list[RestoredMessage]
