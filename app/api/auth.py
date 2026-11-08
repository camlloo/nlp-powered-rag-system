"""认证接口：注册、登录、当前用户。

POST /auth/register  创建用户，组织成员身份由管理员分配
POST /auth/login     校验口令，签发 JWT
GET  /auth/me        当前用户与组织标签
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field, StringConstraints, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.auth import authenticate, hash_password, issue_jwt, verify_password
from app.core.config import get_settings
from app.core.errors import Conflict, Forbidden, NotFound, Unauthorized
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


@router.post("/auth/register", status_code=201, response_model=AuthResponse)
def register(payload: RegisterRequest) -> AuthResponse:
    if payload.org_tags:
        raise Forbidden("组织成员身份须由管理员在用户管理中分配")
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
        token = issue_jwt(user.id, user.username, settings)
        session.commit()
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
