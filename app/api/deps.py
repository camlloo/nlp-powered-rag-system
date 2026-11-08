"""路由依赖项：数据库会话、认证主体与统一授权校验点。

实现文档 §5.3：授权校验点是预留接口——后期以 JWT RBAC 启用时，
只在本模块内实现成员与角色判断，业务模块与路由不改动。
"""

from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.auth import Principal, authenticate, role_at_least
from app.core.config import Settings, get_settings
from app.core.errors import Forbidden, NotFound
from app.db.models import KbCollection
from app.db.session import get_session

SessionDep = Annotated[Session, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def _current_principal(request: Request) -> Principal:
    return authenticate(request)


PrincipalDep = Annotated[Principal, Depends(_current_principal)]


def require_admin(principal: PrincipalDep) -> Principal:
    """全局管理员校验点，管理面接口专用。"""
    if not principal.is_admin:
        raise Forbidden("需要管理员权限")
    return principal


AdminDep = Annotated[Principal, Depends(require_admin)]


def require_kb_access(
    kb_id: int, session: SessionDep, principal: PrincipalDep, min_role: str = "viewer"
) -> KbCollection:
    """统一授权校验点（RBAC）：存在性 + 成员资格 + 角色门槛。

    404 按策略隐藏存在性，403 表示是成员但角色不足（实现文档 §9.4）；
    管理员旁路。后期 JWT 换签发方，此校验点不变。
    """
    from app.db.models import KbMember

    kb = session.scalar(
        select(KbCollection).where(
            KbCollection.id == kb_id,
            KbCollection.tenant_id == principal.tenant_id,
            KbCollection.deleted_at.is_(None),
        )
    )
    if kb is None:
        raise NotFound("知识库不存在")
    if principal.can_see_all():
        return kb
    member = session.get(KbMember, (kb_id, principal.user_id))
    if member is None:
        raise Forbidden("你不是该知识库的成员")
    if not role_at_least(member.role, min_role):
        raise Forbidden("当前角色权限不足")
    return kb
