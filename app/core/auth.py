"""认证与授权核心：用户口令（scrypt）、JWT 签发、访问主体。

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


def _jwt_signing_key(settings: Settings) -> str:
    from app.core.errors import ServiceUnavailable

    if settings.jwt_secret and settings.jwt_secret != "dev-jwt-secret-change-me":
        return settings.jwt_secret
    if settings.api_token and settings.api_token != "change-me":
        return hmac.new(
            settings.api_token.encode("utf-8"), b"nlp-rag-jwt-v1", hashlib.sha256
        ).hexdigest()
    raise ServiceUnavailable("请配置 JWT_SECRET 或安全的 API_TOKEN 后使用用户登录")


def issue_jwt(user_id: int, username: str, settings: Settings) -> str:
    import datetime as dt

    payload = {
        "sub": str(user_id),
        "username": username,
        "exp": dt.datetime.now(dt.UTC) + dt.timedelta(days=settings.jwt_expire_days),
    }
    return jwt.encode(payload, _jwt_signing_key(settings), algorithm="HS256")


@dataclass
class Principal:
    """由 JWT 与数据库中的账号权限解析出的已认证主体。"""

    tenant_id: int
    user_id: int
    username: str
    is_admin: bool = False
    org_tag_ids: set[int] = field(default_factory=set)
    org_tag_names: list[str] = field(default_factory=list)

    def can_see_all(self) -> bool:
        """管理员可读取全部文档并管理知识库。"""
        return self.is_admin


def authenticate(request) -> Principal:
    """校验 JWT，并从数据库读取账号及最新权限。"""
    import jwt as pyjwt

    from app.core.errors import Unauthorized
    from app.db.models import AppUser, OrgTag, UserOrgTag
    from app.db.session import SessionLocal

    settings = get_settings()

    auth_header = request.headers.get("authorization", "")
    token = auth_header[7:].strip() if auth_header.lower().startswith("bearer ") else None
    if not token:
        raise Unauthorized()

    try:
        payload = pyjwt.decode(
            token, _jwt_signing_key(settings), algorithms=["HS256"],
            options={"require": ["sub", "exp"]},
        )
        user_id = int(payload["sub"])
        if user_id <= 0 or user_id > 2**63 - 1:
            raise ValueError("用户 ID 无效")
    except (pyjwt.PyJWTError, ValueError, TypeError) as exc:
        raise Unauthorized("访问令牌无效或已过期") from exc

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
