"""首期最小认证：静态 API Token。

实现文档 §5.3：首期不实现独立用户体系与 RBAC；tenant_id 和 user_id 从服务端
凭证解析，不信任请求体中的同名字段。后期引入 JWT 时，仅需替换本模块的凭证
解析逻辑，统一授权校验点（app/api/deps.py 的 require_kb_access）不变。
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass

from fastapi import Request

from app.core.config import Settings
from app.core.errors import Unauthorized

PRINCIPAL_NOTE = "预留：JWT RBAC 启用后在此携带角色信息"


@dataclass(frozen=True)
class Principal:
    tenant_id: int
    user_id: int
    # 预留字段：后期 JWT 解出 role（owner/editor/viewer）后填充
    role: str = "owner"


def _extract_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip() or None
    return request.headers.get("x-api-token") or None


def get_principal(request: Request, settings: Settings) -> Principal:
    token = _extract_token(request)
    if not settings.api_token:
        raise Unauthorized("服务未配置 API_TOKEN，拒绝访问")
    if not token or not hmac.compare_digest(token, settings.api_token):
        raise Unauthorized()
    return Principal(tenant_id=settings.default_tenant_id, user_id=settings.default_user_id)
