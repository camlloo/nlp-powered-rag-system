"""管理面接口的请求响应模型：组织标签与用户管理（管理员专用）。"""

from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints


class OrgTagCreateRequest(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]


class OrgTagRenameRequest(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]


class OrgTagItem(BaseModel):
    tag_id: int
    name: str
    user_count: int
    document_count: int
    created_at: object | None = None


class OrgTagListResponse(BaseModel):
    total: int
    items: list[OrgTagItem]


class OrgTagDeleted(BaseModel):
    tag_id: int
    detached_documents: int  # 剥离引用的文档数（含软删）
    kbs_touched: list[int]  # 因可见性变化递增 acl_revision 的知识库


class AdminUserItem(BaseModel):
    user_id: int
    username: str
    display_name: str
    is_admin: bool
    org_tag_ids: list[int] = Field(default_factory=list)
    org_tags: list[str] = Field(default_factory=list)
    created_at: object | None = None


class UserListResponse(BaseModel):
    total: int
    items: list[AdminUserItem]


class UserAdminPatchRequest(BaseModel):
    is_admin: bool


class UserTagsPutRequest(BaseModel):
    tag_ids: list[int] = Field(max_length=20)
