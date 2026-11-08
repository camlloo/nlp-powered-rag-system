"""管理面接口：组织标签管理与用户管理（管理员专用，实现文档 §5.3 权限预留的落地）。

- GET/POST /admin/org-tags            标签清单（含使用计数）/ 创建
- PATCH/DELETE /admin/org-tags/{id}   重命名 / 删除（剥离文档与块引用，递增 acl_revision）
- GET  /admin/users                   条件查询与分页清单
- DELETE /admin/users/{id}            删除账号与成员关系，保留文档与审计
- PATCH /admin/users/{id}             授予/撤销管理员（保留最后一名）
- PUT  /admin/users/{id}/org-tags     整体替换用户标签集合

标签删除会改动块级 denormalized `org_tag_id`，属授权变化：受影响知识库在同一
事务中递增 `acl_revision`（文档 §5.3 预留机制），运行时索引缓存随之下一次
请求自动重载（retrieval 每请求重读修订号）。
"""

from __future__ import annotations

from fastapi import APIRouter, Query
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError

from app.api.deps import AdminDep, SessionDep
from app.core.errors import Conflict, NotFound
from app.db.models import (
    AppUser,
    Conversation,
    IngestJob,
    KbChunk,
    KbCollection,
    KbDocument,
    KbMember,
    OrgTag,
    UserOrgTag,
)
from app.db.spaces import get_or_create_main_space
from app.schemas.admin import (
    AdminUserItem,
    OrgTagCreateRequest,
    OrgTagDeleted,
    OrgTagItem,
    OrgTagListResponse,
    OrgTagRenameRequest,
    UserAdminPatchRequest,
    UserListResponse,
    UserTagsPutRequest,
)
from app.schemas.api import ReindexAccepted

router = APIRouter(prefix="/admin")


@router.post("/reindex", status_code=202, response_model=ReindexAccepted)
def reindex_main_space(session: SessionDep, _: AdminDep) -> ReindexAccepted:
    """全量重建主空间索引（去知识库化后的唯一重建入口，管理员专用）。"""
    kb = get_or_create_main_space(session)
    job = IngestJob(kb_id=kb.id, job_type="reindex", payload_json={})
    session.add(job)
    session.commit()
    return ReindexAccepted(job_id=job.id, kb_id=kb.id)


def _load_tag_counts(session) -> tuple[dict[int, int], dict[int, int]]:
    """一次性取标签的用户数与活跃文档数，避免清单页 N+1。"""
    user_counts: dict[int, int] = dict(
        session.execute(
            select(UserOrgTag.org_tag_id, func.count()).group_by(UserOrgTag.org_tag_id)
        ).all()
    )
    doc_counts: dict[int, int] = dict(
        session.execute(
            select(KbDocument.org_tag_id, func.count())
            .where(
                KbDocument.org_tag_id.isnot(None),
                KbDocument.deleted_at.is_(None),
            )
            .group_by(KbDocument.org_tag_id)
        ).all()
    )
    return user_counts, doc_counts


def _tag_item(tag: OrgTag, user_counts: dict[int, int], doc_counts: dict[int, int]) -> OrgTagItem:
    return OrgTagItem(
        tag_id=tag.id,
        name=tag.name,
        user_count=user_counts.get(tag.id, 0),
        document_count=doc_counts.get(tag.id, 0),
        created_at=tag.created_at,
    )


@router.get("/org-tags", response_model=OrgTagListResponse)
def list_org_tags(session: SessionDep, _: AdminDep) -> OrgTagListResponse:
    tags = session.scalars(select(OrgTag).order_by(OrgTag.id)).all()
    user_counts, doc_counts = _load_tag_counts(session)
    return OrgTagListResponse(
        total=len(tags),
        items=[_tag_item(tag, user_counts, doc_counts) for tag in tags],
    )


@router.post("/org-tags", status_code=201, response_model=OrgTagItem)
def create_org_tag(
    payload: OrgTagCreateRequest, session: SessionDep, _: AdminDep
) -> OrgTagItem:
    if session.scalar(select(OrgTag).where(OrgTag.name == payload.name)) is not None:
        raise Conflict("同名组织标签已存在")
    tag = OrgTag(name=payload.name)
    session.add(tag)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        if exc.orig.diag.constraint_name == "uq_org_tag_name":
            raise Conflict("同名组织标签已存在") from exc
        raise
    session.refresh(tag)
    return _tag_item(tag, {}, {})


@router.patch("/org-tags/{tag_id}", response_model=OrgTagItem)
def rename_org_tag(
    tag_id: int, payload: OrgTagRenameRequest, session: SessionDep, _: AdminDep
) -> OrgTagItem:
    tag = session.get(OrgTag, tag_id)
    if tag is None:
        raise NotFound("组织标签不存在")
    if payload.name != tag.name:
        if session.scalar(select(OrgTag).where(OrgTag.name == payload.name)) is not None:
            raise Conflict("同名组织标签已存在")
        tag.name = payload.name
        try:
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            if exc.orig.diag.constraint_name == "uq_org_tag_name":
                raise Conflict("同名组织标签已存在") from exc
            raise
        session.refresh(tag)
    user_counts, doc_counts = _load_tag_counts(session)
    return _tag_item(tag, user_counts, doc_counts)


@router.delete("/org-tags/{tag_id}", response_model=OrgTagDeleted)
def delete_org_tag(tag_id: int, session: SessionDep, _: AdminDep) -> OrgTagDeleted:
    tag = session.get(OrgTag, tag_id)
    if tag is None:
        raise NotFound("组织标签不存在")
    tag_pk = tag.id

    # 先摸清引用面：文档（含软删）与块的去授权化范围、需要失效缓存的 KB
    doc_rows = session.execute(
        select(KbDocument.id, KbDocument.kb_id).where(KbDocument.org_tag_id == tag.id)
    ).all()
    chunk_kb_ids = set(
        session.scalars(
            select(KbChunk.kb_id).where(KbChunk.org_tag_id == tag.id).distinct()
        ).all()
    )
    doc_kb_ids = {kb_id for _, kb_id in doc_rows}
    kbs_touched = sorted(doc_kb_ids | chunk_kb_ids)

    session.execute(
        update(KbDocument).where(KbDocument.org_tag_id == tag.id).values(org_tag_id=None)
    )
    session.execute(
        update(KbChunk).where(KbChunk.org_tag_id == tag.id).values(org_tag_id=None)
    )
    session.execute(delete(UserOrgTag).where(UserOrgTag.org_tag_id == tag.id))
    session.delete(tag)
    if kbs_touched:
        # 授权变化：递增 acl_revision 使运行时索引缓存失效重载（文档 §5.3）
        session.execute(
            update(KbCollection)
            .where(KbCollection.id.in_(kbs_touched))
            .values(acl_revision=KbCollection.acl_revision + 1)
        )
    session.commit()
    return OrgTagDeleted(
        tag_id=tag_pk,
        detached_documents=len(doc_rows),
        kbs_touched=kbs_touched,
    )


def _user_item(session, user: AppUser) -> AdminUserItem:
    tag_rows = session.execute(
        select(UserOrgTag.org_tag_id, OrgTag.name)
        .join(OrgTag, OrgTag.id == UserOrgTag.org_tag_id)
        .where(UserOrgTag.user_id == user.id)
        .order_by(UserOrgTag.org_tag_id)
    ).all()
    return AdminUserItem(
        user_id=user.id,
        username=user.username,
        display_name=user.display_name,
        is_admin=user.is_admin,
        org_tag_ids=[row[0] for row in tag_rows],
        org_tags=[row[1] for row in tag_rows],
        created_at=user.created_at,
    )


def _require_user(session, user_id: int) -> AppUser:
    user = session.scalar(
        select(AppUser).where(AppUser.id == user_id).with_for_update()
    )
    if user is None:
        raise NotFound("用户不存在")
    return user


@router.get("/users", response_model=UserListResponse)
def list_users(
    session: SessionDep,
    _: AdminDep,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    keyword: str = Query(default="", max_length=100),
    is_admin: bool | None = None,
    org_tag_ids: list[int] | None = Query(default=None),
) -> UserListResponse:
    filters = []
    if keyword := keyword.strip():
        filters.append(or_(
            AppUser.username.icontains(keyword, autoescape=True),
            AppUser.display_name.icontains(keyword, autoescape=True),
        ))
    if is_admin is not None:
        filters.append(AppUser.is_admin == is_admin)
    if org_tag_ids:
        # 子查询避免多标签用户重复出现，IN 表达任意一个标签匹配即可。
        filters.append(AppUser.id.in_(
            select(UserOrgTag.user_id).where(UserOrgTag.org_tag_id.in_(set(org_tag_ids)))
        ))
    total = session.scalar(select(func.count()).select_from(AppUser).where(*filters))
    rows = session.scalars(
        select(AppUser).where(*filters).order_by(AppUser.id).offset(offset).limit(limit)
    ).all()
    return UserListResponse(
        total=total,
        items=[_user_item(session, user) for user in rows],
    )


def _lock_admin_ids(session) -> list[int]:
    """删除与变更管理员权限共用锁序，串行复核最后管理员保护。"""
    return list(session.scalars(
        select(AppUser.id).where(AppUser.is_admin.is_(True))
        .order_by(AppUser.id).with_for_update()
    ))


@router.patch("/users/{user_id}", response_model=AdminUserItem)
def update_user_admin(
    user_id: int, payload: UserAdminPatchRequest, session: SessionDep, _: AdminDep
) -> AdminUserItem:
    admin_ids = _lock_admin_ids(session)
    user = _require_user(session, user_id)
    if user.is_admin and not payload.is_admin:
        if len(admin_ids) <= 1:
            raise Conflict("至少保留一名管理员")
    user.is_admin = payload.is_admin
    session.commit()
    session.refresh(user)
    return _user_item(session, user)


@router.delete("/users/{user_id}", status_code=204)
def delete_user(user_id: int, session: SessionDep, principal: AdminDep) -> None:
    admin_ids = _lock_admin_ids(session)
    user = _require_user(session, user_id)
    if user.is_admin and len(admin_ids) <= 1:
        raise Conflict("至少保留一名管理员")
    if user.id == principal.user_id:
        raise Conflict("不能删除当前登录账号")
    session.execute(delete(UserOrgTag).where(UserOrgTag.user_id == user.id))
    session.execute(delete(KbMember).where(KbMember.user_id == user.id))
    session.execute(delete(Conversation).where(Conversation.user_id == user.id))
    session.delete(user)
    session.commit()


@router.put("/users/{user_id}/org-tags", response_model=AdminUserItem)
def update_user_tags(
    user_id: int, payload: UserTagsPutRequest, session: SessionDep, _: AdminDep
) -> AdminUserItem:
    user = _require_user(session, user_id)
    wanted = sorted(set(payload.tag_ids))
    if wanted:
        found = set(
            session.scalars(select(OrgTag.id).where(OrgTag.id.in_(wanted))).all()
        )
        missing = [tag_id for tag_id in wanted if tag_id not in found]
        if missing:
            raise NotFound(f"组织标签不存在：{missing[0]}")
    current = set(
        session.scalars(
            select(UserOrgTag.org_tag_id).where(UserOrgTag.user_id == user.id)
        ).all()
    )
    for stale_id in current - set(wanted):
        session.delete(session.get(UserOrgTag, (user.id, stale_id)))
    for fresh_id in set(wanted) - current:
        session.add(UserOrgTag(user_id=user.id, org_tag_id=fresh_id))
    session.commit()
    session.refresh(user)
    return _user_item(session, user)
