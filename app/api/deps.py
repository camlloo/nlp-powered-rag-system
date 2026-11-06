"""路由依赖项：数据库会话、认证主体与统一授权校验点。

实现文档 §5.3：授权校验点是预留接口——后期以 JWT RBAC 启用时，
只在本模块内实现成员与角色判断，业务模块与路由不改动。
"""

from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.auth import Principal, get_principal
from app.core.config import Settings, get_settings
from app.core.errors import NotFound
from app.db.models import KbCollection
from app.db.session import get_session

SessionDep = Annotated[Session, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def _current_principal(request: Request, settings: SettingsDep) -> Principal:
    return get_principal(request, settings)


PrincipalDep = Annotated[Principal, Depends(_current_principal)]


def require_kb_access(kb_id: int, session: SessionDep, principal: PrincipalDep) -> KbCollection:
    """统一授权校验点：首期仅校验知识库存在且属于当前租户。

    404 按策略隐藏存在性（实现文档 §9.4）；后期 RBAC 在此补充角色判断与
    acl_revision 失效检查。
    """
    kb = session.scalar(
        select(KbCollection).where(
            KbCollection.id == kb_id,
            KbCollection.tenant_id == principal.tenant_id,
        )
    )
    if kb is None:
        raise NotFound("知识库不存在")
    return kb
