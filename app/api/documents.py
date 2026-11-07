"""文档接口（去知识库化产品面）：上传归属「个人 / 某组织」+ 公开/私有。

- POST   /documents：multipart 上传，scope=personal 或组织名，visibility=public/private；
- GET    /documents：全局文档清单（按用户可见性过滤，附归属组织名）；
- PATCH  /documents/{id}：修改归属与可见性（块级冗余列同步，acl_revision 递增）。

内部落到单一「主空间」（app/db/spaces.py）——快照索引与任务机器以 kb_id 为键，
组织/个人与公开/私有的检索语义完全由文档级字段承载：
public → 所有人；private+org_tag → 上传者与组织成员；private 无标签 → 仅上传者。
"""

from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath

from fastapi import APIRouter, File, Form, Header, Query, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from app.api.deps import PrincipalDep, SessionDep, SettingsDep, require_kb_access
from app.api.idempotency import idempotency_store
from app.core.auth import Principal
from app.core.config import Settings
from app.core.errors import Conflict, Forbidden, NotFound, ValidationFailed
from app.db.models import IngestJob, KbChunk, KbCollection, KbDocument, KbDocumentVersion, OrgTag
from app.db.spaces import get_or_create_main_space
from app.parsers.base import check_size, detect_format
from app.parsers.intake import save_upload
from app.schemas.api import (
    DocumentItem,
    DocumentPatchRequest,
    DocumentUploaded,
    GlobalDocumentListResponse,
)

router = APIRouter()


def create_document_record(
    *,
    session: Session,
    principal: Principal,
    settings: Settings,
    kb: KbCollection,
    org_tag_id: int | None,
    filename: str,
    content_type: str | None,
    payload: bytes,
    fmt: str,
    visibility: str,
    idempotency_key: str | None,
    route: str,
) -> DocumentUploaded | JSONResponse:
    """上传登记共享核心（/documents 与 /kbs/{id}/documents 共用）。

    幂等键回放、SHA-256 重复检测、文档+版本落库、data_revision 递增与
    ingest_file 任务创建在同一请求内完成；任务 payload 携带 uploader_user_id
    供无成员身份的上传者轮询进度。
    """
    request_digest = hashlib.sha256(
        b"|".join(
            [
                str(principal.user_id).encode(),
                str(kb.id).encode(),
                filename.encode("utf-8", "replace"),
                len(payload).to_bytes(8, "big"),
                hashlib.sha256(payload).digest(),
            ]
        )
    ).hexdigest()

    if idempotency_key:
        outcome, record = idempotency_store.check(
            key=idempotency_key, user_id=principal.user_id, route=route, digest=request_digest
        )
        if outcome == "replay":
            return JSONResponse(
                status_code=record.status_code,
                content=record.body,
                headers={"Idempotent-Replay": "true"},
            )
        if outcome == "conflict":
            raise Conflict("相同 Idempotency-Key 但请求内容不同")

    # 文件级 SHA-256 重复检测（实现文档 §4.4）：同一空间内已有相同内容的
    # 未删除文件时不再重复登记与索引
    payload_digest = hashlib.sha256(payload).digest()
    duplicate_version = session.execute(
        select(KbDocumentVersion)
        .join(KbDocument, KbDocument.id == KbDocumentVersion.document_id)
        .where(
            KbDocumentVersion.kb_id == kb.id,
            KbDocumentVersion.file_sha256 == payload_digest,
            KbDocument.deleted_at.is_(None),
        )
        .limit(1)
    ).scalar_one_or_none()
    if duplicate_version is not None:
        body = DocumentUploaded(
            job_id=None,
            document_id=duplicate_version.document_id,
            version_no=duplicate_version.version_no,
            kb_id=kb.id,
            duplicate=True,
        )
        return JSONResponse(status_code=200, content=body.model_dump())

    storage_uri, sha_hex = save_upload(
        payload, fmt, upload_dir=Path(settings.storage_dir) / "uploads"
    )
    title = PurePosixPath(filename).stem or "未命名文档"
    document = KbDocument(
        kb_id=kb.id,
        title=title,
        uploader_user_id=principal.user_id,
        visibility=visibility,
        org_tag_id=org_tag_id,
    )
    session.add(document)
    session.flush()
    version = KbDocumentVersion(
        kb_id=kb.id,
        document_id=document.id,
        version_no=1,
        title=title,
        file_sha256=bytes.fromhex(sha_hex),
        storage_uri=storage_uri,
        parse_status="pending",
    )
    session.add(version)
    kb.data_revision += 1  # 新文件加入清单：与登记同一事务递增（实现文档 §5.3）
    session.flush()
    job = IngestJob(
        kb_id=kb.id,
        doc_version_id=version.id,
        job_type="ingest_file",
        payload_json={
            "filename": filename,
            "content_type": content_type,
            "format": fmt,
            "uploader_user_id": principal.user_id,
        },
    )
    session.add(job)
    session.commit()

    body = DocumentUploaded(
        job_id=job.id, document_id=document.id, version_no=version.version_no, kb_id=kb.id
    )
    if idempotency_key:
        idempotency_store.save(
            key=idempotency_key,
            user_id=principal.user_id,
            route=route,
            digest=request_digest,
            status_code=202,
            body=body.model_dump(),
        )
    return body


def _resolve_org_scope(session: Session, principal: Principal, scope: str) -> int | None:
    """scope → org_tag_id：personal 归个人（无标签），组织名需成员或管理员。"""
    if scope == "personal":
        return None
    name = scope.strip()
    if not name:
        raise ValidationFailed("scope 仅支持 personal 或组织名称")
    tag = session.scalar(select(OrgTag).where(OrgTag.name == name))
    if tag is None:
        raise NotFound(f"组织标签不存在：{name}")
    if not principal.can_see_all() and tag.id not in principal.org_tag_ids:
        raise Forbidden("只能向自己所在的组织添加文档")
    return tag.id


@router.post("/documents", status_code=202, response_model=DocumentUploaded)
async def upload_document(
    session: SessionDep,
    principal: PrincipalDep,
    settings: SettingsDep,
    file: UploadFile = File(...),
    scope: str = Form(default="personal"),
    visibility: str = Form(default="private"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> object:
    """上传文档：归属个人或组织，公开/私有决定检索可见范围。"""
    if visibility not in ("private", "public"):
        raise ValidationFailed("visibility 仅支持 private / public")
    filename = file.filename or ""
    payload = await file.read()
    check_size(len(payload), settings)  # 413
    fmt = detect_format(filename, file.content_type, payload)  # 422
    org_tag_id = _resolve_org_scope(session, principal, scope)
    kb = get_or_create_main_space(session)
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
        route="documents:main",
    )


def _visibility_filters(principal: Principal) -> list:
    """可见性：自己的 ∨ 公开 ∨ 组织标签匹配（管理员旁路，与检索过滤一致）。"""
    if principal.can_see_all():
        return []
    visibility_filters = [KbDocument.uploader_user_id == principal.user_id]
    visibility_filters.append(KbDocument.visibility == "public")
    if own_org_ids := sorted(principal.org_tag_ids):
        visibility_filters.append(KbDocument.org_tag_id.in_(own_org_ids))
    return [or_(*visibility_filters)]


@router.get("/documents", response_model=GlobalDocumentListResponse)
def list_documents(
    session: SessionDep,
    principal: PrincipalDep,
    include_deleted: bool = False,
    scope: str = Query(default="all", description="all | personal | 组织名称"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> GlobalDocumentListResponse:
    """全局文档清单：仅返回当前用户可见的文档（归属/可见性随行返回）。"""
    filters = []
    if not include_deleted:
        filters.append(KbDocument.deleted_at.is_(None))
    filters.extend(_visibility_filters(principal))
    if scope == "personal":
        filters.append(KbDocument.org_tag_id.is_(None))
    elif scope != "all":
        tag = session.scalar(select(OrgTag).where(OrgTag.name == scope.strip()))
        if tag is None:
            raise NotFound(f"组织标签不存在：{scope}")
        filters.append(KbDocument.org_tag_id == tag.id)

    total = session.execute(
        select(func.count()).select_from(KbDocument).where(*filters)
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
    return GlobalDocumentListResponse(total=total, items=items)


def _load_document_row(session: Session, document_id: int):
    """文档 + 当前版本 + 归属组织名（不存在返回 None）。"""
    return session.execute(
        select(KbDocument, KbDocumentVersion, OrgTag.name)
        .join(
            KbDocumentVersion,
            (KbDocumentVersion.document_id == KbDocument.id)
            & (KbDocumentVersion.version_no == KbDocument.current_version_no),
        )
        .outerjoin(OrgTag, OrgTag.id == KbDocument.org_tag_id)
        .where(KbDocument.id == document_id)
    ).first()


def _document_item(
    document: KbDocument, version: KbDocumentVersion, org_name: str | None
) -> DocumentItem:
    return DocumentItem(
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


@router.patch("/documents/{document_id}", response_model=DocumentItem)
def update_document(
    document_id: int,
    payload: DocumentPatchRequest,
    session: SessionDep,
    principal: PrincipalDep,
) -> DocumentItem:
    """修改归属与可见性（上传者本人 ∨ 管理员 ∨ 原知识库 editor）。

    改动会同步块级冗余列（org_tag_id / visibility），并递增 kb.acl_revision
    ——授权变化使运行时索引缓存重载（实现文档 §5.3），检索可见范围即时生效。
    """
    if payload.scope is None and payload.visibility is None:
        raise ValidationFailed("至少提供 scope 或 visibility 之一")
    document = session.get(KbDocument, document_id)
    if document is None:
        raise NotFound("文档不存在")
    if not principal.can_see_all() and document.uploader_user_id != principal.user_id:
        require_kb_access(
            kb_id=document.kb_id, session=session, principal=principal, min_role="editor"
        )
    if document.deleted_at is not None:
        raise Conflict("文档已删除")

    if payload.scope is not None:
        document.org_tag_id = _resolve_org_scope(session, principal, payload.scope)
    if payload.visibility is not None:
        if payload.visibility not in ("private", "public"):
            raise ValidationFailed("visibility 仅支持 private / public")
        document.visibility = payload.visibility

    # 块级冗余列随文档同步（写入与向量同批的检索级过滤依据）
    session.execute(
        update(KbChunk)
        .where(KbChunk.document_id == document.id, KbChunk.kb_id == document.kb_id)
        .values(org_tag_id=document.org_tag_id, visibility=document.visibility)
    )
    # 授权变化：递增 acl_revision 使运行时索引缓存失效重载
    kb = session.get(KbCollection, document.kb_id)
    kb.acl_revision += 1
    session.commit()

    row = _load_document_row(session, document_id)
    assert row is not None
    return _document_item(*row)
