"""知识库/文件/任务接口的请求响应模型（实现文档 §9.2/§9.4）。"""

from pydantic import BaseModel, Field


class KbCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class KbItem(BaseModel):
    kb_id: int
    name: str
    has_index: bool


class KbListResponse(BaseModel):
    items: list[KbItem]


class KbCreated(BaseModel):
    kb_id: int
    name: str


class KbDeleted(BaseModel):
    kb_id: int
    deleted_at: object
    cleanup_job_id: int


class DocumentUploaded(BaseModel):
    job_id: int | None  # 重复上传（SHA-256 命中）时为 None
    document_id: int
    version_no: int
    kb_id: int
    duplicate: bool = False


class DocumentItem(BaseModel):
    document_id: int
    title: str
    version_no: int
    parse_status: str
    visibility: str = "public"
    org_tag_id: int | None = None  # None = 个人归属
    org_tag_name: str | None = None
    uploader_user_id: int | None = None  # 前端据此判断可否编辑归属/可见性
    uploader_username: str | None = None  # 个人归属时前端展示归属用户名
    deleted_at: object | None = None
    created_at: object | None = None


class DocumentPatchRequest(BaseModel):
    """修改文档归属与可见性；两者至少提供其一。"""

    scope: str | None = None  # personal 或组织名称
    visibility: str | None = None  # public / private


class DocumentListResponse(BaseModel):
    kb_id: int
    total: int
    items: list[DocumentItem]


class GlobalDocumentListResponse(BaseModel):
    """去知识库化的全局文档清单（POST /documents 配套）。"""

    total: int
    items: list[DocumentItem]


class DocumentDeleted(BaseModel):
    document_id: int
    deleted_at: object
    reindex_job_id: int
    cleanup_job_id: int


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
