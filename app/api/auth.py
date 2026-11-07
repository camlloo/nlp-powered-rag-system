"""认证接口：注册、登录、当前用户。

POST /auth/register  创建用户并打组织标签（按名称 get-or-create）
POST /auth/login     校验口令，签发 JWT
GET  /auth/me        当前用户与组织标签
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field, StringConstraints, field_validator
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError

from app.core.auth import authenticate, hash_password, issue_jwt, verify_password
from app.core.config import get_settings
from app.core.errors import Conflict, NotFound, Unauthorized
from app.db.models import AppUser, OrgTag, UserOrgTag
from app.db.session import SessionLocal

router = APIRouter()


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern="^[a-zA-Z0-9_.-]+$")
    password: str = Field(min_length=6, max_length=100)
    display_name: str = Field(min_length=1, max_length=100)
    org_tags: list[Annotated[str, StringConstraints(strip_whitespace=True, max_length=64)]] = Field(
        default_factory=list, max_length=10
    )

    @field_validator("org_tags")
    @classmethod
    def normalize_org_tags(cls, names: list[str]) -> list[str]:
        return list(dict.fromkeys(name for name in names if name))


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=100)


class MeResponse(BaseModel):
    user_id: int
    username: str
    display_name: str
    is_admin: bool
    org_tags: list[str]


class AuthResponse(BaseModel):
    access_token: str
    user: MeResponse


def _get_or_create_org_tags(session, names: list[str]) -> list[int]:
    ids: list[int] = []
    for name in names:
        name = name.strip()
        if not name:
            continue
        session.execute(insert(OrgTag).values(name=name).on_conflict_do_nothing(index_elements=["name"]))
        ids.append(session.scalar(select(OrgTag.id).where(OrgTag.name == name)))
    return ids


@router.post("/auth/register", status_code=201, response_model=AuthResponse)
def register(payload: RegisterRequest) -> AuthResponse:
    settings = get_settings()
    with SessionLocal() as session:
        exists = session.scalar(select(AppUser).where(AppUser.username == payload.username))
        if exists is not None:
            raise Conflict("用户名已被注册")
        user = AppUser(
            username=payload.username,
            password_hash=hash_password(payload.password),
            display_name=payload.display_name or payload.username,
        )
        session.add(user)
        try:
            session.flush()
        except IntegrityError as exc:
            if exc.orig.diag.constraint_name == "uq_app_user_username":
                raise Conflict("用户名已被注册") from exc
            raise
        tag_ids = _get_or_create_org_tags(session, payload.org_tags)
        for tag_id in tag_ids:
            session.add(UserOrgTag(user_id=user.id, org_tag_id=tag_id))
        session.commit()
        token = issue_jwt(user.id, user.username, settings)
        return AuthResponse(
            access_token=token,
            user=MeResponse(
                user_id=user.id,
                username=user.username,
                display_name=user.display_name,
                is_admin=user.is_admin,
                org_tags=payload.org_tags,
            ),
        )


@router.post("/auth/login", response_model=AuthResponse)
def login(payload: LoginRequest) -> AuthResponse:
    settings = get_settings()
    with SessionLocal() as session:
        user = session.scalar(select(AppUser).where(AppUser.username == payload.username))
        if user is None or not verify_password(payload.password, user.password_hash):
            raise Unauthorized("用户名或密码错误")
        org_rows = session.execute(
            select(OrgTag.name)
            .join(UserOrgTag, UserOrgTag.org_tag_id == OrgTag.id)
            .where(UserOrgTag.user_id == user.id)
        ).scalars().all()
        token = issue_jwt(user.id, user.username, settings)
        return AuthResponse(
            access_token=token,
            user=MeResponse(
                user_id=user.id,
                username=user.username,
                display_name=user.display_name,
                is_admin=user.is_admin,
                org_tags=list(org_rows),
            ),
        )


@router.get("/auth/me", response_model=MeResponse)
def me(request: Request) -> MeResponse:
    principal = authenticate(request)
    if principal.is_admin:
        # 静态令牌旁路 = 预置管理员：始终返回旁路档案，不落回同 id 的普通用户行
        return MeResponse(
            user_id=principal.user_id,
            username=principal.username,
            display_name=principal.username,
            is_admin=True,
            org_tags=[],
        )
    with SessionLocal() as session:
        user = session.get(AppUser, principal.user_id)
        if user is None:
            raise NotFound("用户不存在")
        return MeResponse(
            user_id=user.id,
            username=user.username,
            display_name=user.display_name,
            is_admin=user.is_admin,
            org_tags=principal.org_tag_names,
        )
