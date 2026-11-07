"""知识库与文件管理接口：M4 最小切片（实现文档 §9.2）。

- POST /kbs：创建知识库；
- POST /kbs/{id}/documents：multipart 上传，202 + job_id，支持 Idempotency-Key；
- POST /kbs/{id}/reindex：创建全量重建任务。
"""

from __future__ import annotations

import datetime as dt
from datetime import datetime

from fastapi import APIRouter, File, Form, Header, Query, UploadFile
from sqlalchemy import func, or_, select

from app.api.deps import PrincipalDep, SessionDep, SettingsDep, require_kb_access
from app.api.documents import create_document_record
from app.core.errors import AppError, Conflict, NotFound, ValidationFailed
from app.db.models import IngestJob, KbCollection, KbDocument, KbDocumentVersion, KbMember, OrgTag
from app.db.spaces import MAIN_SPACE_NAME
from app.parsers.base import check_size, detect_format
from app.schemas.api import (
    DocumentDeleted,
    DocumentItem,
    DocumentListResponse,
    DocumentUploaded,
    KbCreated,
    KbCreateRequest,
    KbDeleted,
    KbItem,
    KbListResponse,
    ReindexAccepted,
)

router = APIRouter()


@router.get("/kbs", response_model=KbListResponse)
def list_kbs(session: SessionDep, principal: PrincipalDep) -> KbListResponse:
    """当前租户的知识库清单（供前端侧栏使用）。"""
    query = (
        select(KbCollection)
        .where(
            KbCollection.tenant_id == principal.tenant_id,
            KbCollection.deleted_at.is_(None),
        )
        .order_by(KbCollection.id)
    )
    if not principal.can_see_all():
        query = query.join(KbMember, KbMember.kb_id == KbCollection.id).where(
            KbMember.user_id == principal.user_id
        )
    rows = session.scalars(query).all()
    return KbListResponse(
        items=[
            KbItem(kb_id=kb.id, name=kb.name, has_index=kb.active_index_id is not None)
            for kb in rows
        ]
    )


@router.post("/kbs", status_code=201, response_model=KbCreated)
def create_kb(
    payload: KbCreateRequest, session: SessionDep, principal: PrincipalDep
) -> KbCreated:
    if payload.name.strip() == MAIN_SPACE_NAME:
        raise Conflict("该名称为系统保留")
    kb = KbCollection(tenant_id=principal.tenant_id, name=payload.name)
    session.add(kb)
    session.flush()
    # kb_member 为预留表：写入 owner 关系，后期 JWT RBAC 启用时直接生效
    session.add(KbMember(kb_id=kb.id, user_id=principal.user_id, role="owner"))
    session.commit()
    return KbCreated(kb_id=kb.id, name=kb.name)


@router.post("/kbs/{kb_id}/documents", status_code=202, response_model=DocumentUploaded)
async def upload_document(
    kb_id: int,
    session: SessionDep,
    principal: PrincipalDep,
    settings: SettingsDep,
    file: UploadFile = File(...),
    visibility: str = Form(default="private"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> object:
    kb = require_kb_access(
        kb_id=kb_id, session=session, principal=principal, min_role="editor"
    )
    filename = file.filename or ""
    payload = await file.read()

    check_size(len(payload), settings)  # 413
    fmt = detect_format(filename, file.content_type, payload)  # 422
    if visibility not in ("private", "public"):
        raise ValidationFailed("visibility 仅支持 private / public")

    # 组织标签：随上传者继承（首个组织标签）；检索级过滤依据
    org_tag_id = sorted(principal.org_tag_ids)[0] if principal.org_tag_ids else None
    return create_document_record(
        session=session,
        principal=principal,
        settings=settings,
        kb=kb,
        org_tag_id=org_tag_id,
        filename=filename,
        content_type=file.content_type,
        payload=payload,
        fmt=fmt,
        visibility=visibility,
        idempotency_key=idempotency_key,
        route=f"documents:{kb_id}",
    )


@router.post("/kbs/{kb_id}/reindex", status_code=202, response_model=ReindexAccepted)
def reindex_kb(kb_id: int, session: SessionDep, principal: PrincipalDep) -> ReindexAccepted:
    require_kb_access(kb_id=kb_id, session=session, principal=principal, min_role="editor")
    job = IngestJob(kb_id=kb_id, job_type="reindex", payload_json={})
    session.add(job)
    session.commit()
    return ReindexAccepted(job_id=job.id, kb_id=kb_id)


@router.delete("/kbs/{kb_id}", response_model=KbDeleted)
def delete_kb(kb_id: int, session: SessionDep, principal: PrincipalDep) -> KbDeleted:
    """逻辑删除知识库：同事务写 deleted_at 并递增 data_revision，
    已删知识库的全部接口（检索/问答/文档/引用）立即 404；
    物理清理按保留期异步执行。"""
    kb = require_kb_access(kb_id=kb_id, session=session, principal=principal, min_role="owner")
    if kb.name == MAIN_SPACE_NAME:
        raise Conflict("主空间不可删除")
    if kb.deleted_at is not None:
        raise Conflict("知识库已删除")
    deleted_at = datetime.now(dt.UTC)
    kb.deleted_at = deleted_at
    kb.data_revision += 1
    cleanup_job = IngestJob(
        kb_id=kb_id, job_type="cleanup", payload_json={"reason": "kb_deleted"}
    )
    session.add(cleanup_job)
    session.commit()
    return KbDeleted(kb_id=kb_id, deleted_at=deleted_at, cleanup_job_id=cleanup_job.id)


@router.get("/kbs/{kb_id}/documents", response_model=DocumentListResponse)
def list_documents(
    kb_id: int,
    session: SessionDep,
    principal: PrincipalDep,
    include_deleted: bool = False,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> DocumentListResponse:
    """文档清单及当前版本的处理状态（§9.2）；按用户可见性过滤。"""
    require_kb_access(kb_id=kb_id, session=session, principal=principal)
    filters = [KbDocument.kb_id == kb_id]
    if not include_deleted:
        filters.append(KbDocument.deleted_at.is_(None))
    # 可见性：自己的 ∨ 公开 ∨ 组织标签匹配（管理员旁路）
    if not principal.can_see_all():
        own_org_ids = sorted(principal.org_tag_ids)
        visibility_filters = [KbDocument.uploader_user_id == principal.user_id]
        visibility_filters.append(KbDocument.visibility == "public")
        if own_org_ids:
            visibility_filters.append(KbDocument.org_tag_id.in_(own_org_ids))
        filters.append(or_(*visibility_filters))

    total = session.execute(
        select(func.count())
        .select_from(KbDocument)
        .where(*filters)
    ).scalar_one()
    rows = session.execute(
        select(KbDocument, KbDocumentVersion, OrgTag.name)
        .join(
            KbDocumentVersion,
            (KbDocumentVersion.document_id == KbDocument.id)
            & (KbDocumentVersion.version_no == KbDocument.current_version_no),
        )
        .outerjoin(OrgTag, OrgTag.id == KbDocument.org_tag_id)
        .where(*filters)
        .order_by(KbDocument.id)
        .offset(offset)
        .limit(limit)
    ).all()
    items = [
        DocumentItem(
            document_id=document.id,
            title=document.title,
            version_no=version.version_no,
            parse_status=version.parse_status,
            visibility=document.visibility,
            org_tag_id=document.org_tag_id,
            org_tag_name=org_name,
            uploader_user_id=document.uploader_user_id,
            deleted_at=document.deleted_at,
            created_at=version.created_at,
        )
        for document, version, org_name in rows
    ]
    return DocumentListResponse(kb_id=kb_id, total=total, items=items)


@router.delete("/documents/{document_id}", response_model=DocumentDeleted)
def delete_document(
    document_id: int, session: SessionDep, principal: PrincipalDep
) -> DocumentDeleted:
    """逻辑删除：同事务写 deleted_at 并递增 data_revision（实现文档 §5.3），
    随后登记重建与物理清理任务。权限：管理员 ∨ 上传者本人 ∨ 知识库 editor。"""
    document = session.get(KbDocument, document_id)
    if document is None:
        raise NotFound("文档不存在")
    if (
        not principal.can_see_all()
        and document.uploader_user_id != principal.user_id
    ):
        require_kb_access(
            kb_id=document.kb_id, session=session, principal=principal, min_role="editor"
        )
    if document.deleted_at is not None:
        raise Conflict("文档已删除")

    deleted_at = datetime.now(dt.UTC)
    document.deleted_at = deleted_at
    kb = session.get(KbCollection, document.kb_id)
    kb.data_revision += 1  # 同事务递增：旧内容立即失效（缓存按修订号重载）
    reindex_job = IngestJob(
        kb_id=document.kb_id,
        job_type="reindex",
        payload_json={"reason": "document_deleted", "document_id": document_id},
    )
    cleanup_job = IngestJob(
        kb_id=document.kb_id,
        job_type="cleanup",
        payload_json={"reason": "document_deleted"},
    )
    session.add_all([reindex_job, cleanup_job])
    session.commit()
    return DocumentDeleted(
        document_id=document_id,
        deleted_at=deleted_at,
        reindex_job_id=reindex_job.id,
        cleanup_job_id=cleanup_job.id,
    )


@router.post("/kbs/{kb_id}/members", include_in_schema=True)
def add_member(kb_id: int, session: SessionDep, principal: PrincipalDep) -> None:
    """预留接口：后期以 JWT RBAC 实现（实现文档 §9.2）。"""
    require_kb_access(kb_id=kb_id, session=session, principal=principal)
    raise AppError(
        "成员管理为预留接口，后期以 JWT RBAC 实现",
        code="not_implemented",
        status_code=501,
    )
