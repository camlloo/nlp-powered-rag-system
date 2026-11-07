"""知识库/文件/任务接口的请求响应模型（实现文档 §9.2/§9.4）。"""

from pydantic import BaseModel, Field


class KbCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class KbCreated(BaseModel):
    kb_id: int
    name: str


class DocumentUploaded(BaseModel):
    job_id: int
    document_id: int
    version_no: int
    kb_id: int


class ReindexAccepted(BaseModel):
    job_id: int
    kb_id: int


class JobStatusResponse(BaseModel):
    job_id: int
    kb_id: int
    doc_version_id: int | None
    job_type: str
    status: str
    stage: str | None
    attempt: int
    max_attempt: int
    error_code: str | None
    error_detail: str | None
