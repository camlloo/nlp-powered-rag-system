"""认证与授权核心：用户口令（scrypt）、JWT 签发、访问主体。

- 静态 API Token 保留为管理员旁路（脚本与旧测试兼容）；
- 用户体系：/auth/login 签发 JWT（HS256，7 天）；
- Principal 携带组织标签，供检索级可见性过滤使用；
- RBAC 角色（kb_member.role）：owner > editor > viewer。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass, field

import jwt

from app.core.config import Settings, get_settings

ROLE_ORDER = {"viewer": 0, "editor": 1, "owner": 2}


def role_at_least(role: str | None, min_role: str) -> bool:
    return ROLE_ORDER.get(role or "", -1) >= ROLE_ORDER[min_role]


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_hex, digest_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode("utf-8"), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1, dklen=32
        )
        return hmac.compare_digest(digest, bytes.fromhex(digest_hex))
    except (ValueError, TypeError):
        return False


def issue_jwt(user_id: int, username: str, settings: Settings) -> str:
    import datetime as dt

    payload = {
        "sub": str(user_id),
        "username": username,
        "exp": dt.datetime.now(dt.UTC) + dt.timedelta(days=settings.jwt_expire_days),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


@dataclass
class Principal:
    """已认证访问主体：静态令牌旁路 = 预置管理员；用户体系 = JWT 解出。"""

    tenant_id: int
    user_id: int
    username: str
    is_admin: bool = False
    org_tag_ids: set[int] = field(default_factory=set)
    org_tag_names: list[str] = field(default_factory=list)

    def can_see_all(self) -> bool:
        """管理员旁路：检索过滤与角色门全部放行。"""
        return self.is_admin


def get_admin_principal() -> Principal:
    settings = get_settings()
    return Principal(
        tenant_id=settings.default_tenant_id,
        user_id=settings.default_user_id,
        username=settings.admin_username,
        is_admin=True,
    )


def _extract_static_token(request) -> str | None:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip() or None
    return request.headers.get("x-api-token") or None


def authenticate(request) -> Principal:
    """请求 → Principal：JWT 优先，静态令牌旁路，均失败抛 401。"""
    import jwt as pyjwt

    from app.core.errors import Unauthorized
    from app.db.models import AppUser, OrgTag, UserOrgTag
    from app.db.session import SessionLocal

    settings = get_settings()

    static_token = _extract_static_token(request)
    api_ok = (
        static_token
        and settings.api_token
        and hmac.compare_digest(static_token, settings.api_token)
    )
    if api_ok:
        return get_admin_principal()

    auth_header = request.headers.get("authorization", "")
    token = auth_header[7:].strip() if auth_header.lower().startswith("bearer ") else None
    if not token:
        raise Unauthorized()

    try:
        payload = pyjwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    except pyjwt.PyJWTError as exc:
        raise Unauthorized("访问令牌无效或已过期") from exc

    user_id = int(payload.get("sub", "0"))
    with SessionLocal() as session:
        user = session.get(AppUser, user_id)
        if user is None:
            raise Unauthorized("用户不存在")
        org_rows = session.execute(
            __import__("sqlalchemy")
            .select(OrgTag.id, OrgTag.name)
            .join(UserOrgTag, UserOrgTag.org_tag_id == OrgTag.id)
            .where(UserOrgTag.user_id == user.id)
        ).all()
    return Principal(
        tenant_id=settings.default_tenant_id,
        user_id=user.id,
        username=user.username,
        is_admin=user.is_admin,
        org_tag_ids={row[0] for row in org_rows},
        org_tag_names=[row[1] for row in org_rows],
    )
