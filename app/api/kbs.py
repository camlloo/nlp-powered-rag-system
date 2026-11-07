"""知识库与文件管理接口：M4 最小切片（实现文档 §9.2）。

- POST /kbs：创建知识库；
- POST /kbs/{id}/documents：multipart 上传，202 + job_id，支持 Idempotency-Key；
- POST /kbs/{id}/reindex：创建全量重建任务。
"""

from __future__ import annotations

import datetime as dt
import hashlib
from datetime import datetime
from pathlib import Path, PurePosixPath

from fastapi import APIRouter, File, Header, Query, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy import func, select

from app.api.deps import PrincipalDep, SessionDep, SettingsDep, require_kb_access
from app.api.idempotency import idempotency_store
from app.core.errors import AppError, Conflict, NotFound
from app.db.models import IngestJob, KbCollection, KbDocument, KbDocumentVersion, KbMember
from app.parsers.base import check_size, detect_format
from app.parsers.intake import save_upload
from app.schemas.api import (
    DocumentDeleted,
    DocumentItem,
    DocumentListResponse,
    DocumentUploaded,
    KbCreated,
    KbCreateRequest,
    ReindexAccepted,
)

router = APIRouter()


@router.post("/kbs", status_code=201, response_model=KbCreated)
def create_kb(
    payload: KbCreateRequest, session: SessionDep, principal: PrincipalDep
) -> KbCreated:
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
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> object:
    kb = require_kb_access(kb_id=kb_id, session=session, principal=principal)
    filename = file.filename or ""
    payload = await file.read()

    check_size(len(payload), settings)  # 413
    fmt = detect_format(filename, file.content_type, payload)  # 422

    request_digest = hashlib.sha256(
        b"|".join(
            [
                str(principal.user_id).encode(),
                str(kb_id).encode(),
                filename.encode("utf-8", "replace"),
                len(payload).to_bytes(8, "big"),
                hashlib.sha256(payload).digest(),
            ]
        )
    ).hexdigest()

    route = f"documents:{kb_id}"
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

    # 文件级 SHA-256 重复检测（实现文档 §4.4）：同知识库内已有相同内容的
    # 未删除文件时不再重复登记与索引
    payload_digest = hashlib.sha256(payload).digest()
    duplicate_version = session.execute(
        select(KbDocumentVersion)
        .join(KbDocument, KbDocument.id == KbDocumentVersion.document_id)
        .where(
            KbDocumentVersion.kb_id == kb_id,
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
            kb_id=kb_id,
            duplicate=True,
        )
        return JSONResponse(status_code=200, content=body.model_dump())

    storage_uri, sha_hex = save_upload(
        payload, fmt, upload_dir=Path(settings.storage_dir) / "uploads"
    )
    title = PurePosixPath(filename).stem or "未命名文档"
    document = KbDocument(kb_id=kb_id, title=title)
    session.add(document)
    session.flush()
    version = KbDocumentVersion(
        kb_id=kb_id,
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
        kb_id=kb_id,
        doc_version_id=version.id,
        job_type="ingest_file",
        payload_json={"filename": filename, "content_type": file.content_type, "format": fmt},
    )
    session.add(job)
    session.commit()

    body = DocumentUploaded(
        job_id=job.id, document_id=document.id, version_no=version.version_no, kb_id=kb_id
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


@router.post("/kbs/{kb_id}/reindex", status_code=202, response_model=ReindexAccepted)
def reindex_kb(kb_id: int, session: SessionDep, principal: PrincipalDep) -> ReindexAccepted:
    require_kb_access(kb_id=kb_id, session=session, principal=principal)
    job = IngestJob(kb_id=kb_id, job_type="reindex", payload_json={})
    session.add(job)
    session.commit()
    return ReindexAccepted(job_id=job.id, kb_id=kb_id)


@router.get("/kbs/{kb_id}/documents", response_model=DocumentListResponse)
def list_documents(
    kb_id: int,
    session: SessionDep,
    principal: PrincipalDep,
    include_deleted: bool = False,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> DocumentListResponse:
    """文档清单及当前版本的处理状态（§9.2）。"""
    require_kb_access(kb_id=kb_id, session=session, principal=principal)
    filters = [KbDocument.kb_id == kb_id]
    if not include_deleted:
        filters.append(KbDocument.deleted_at.is_(None))

    total = session.execute(
        select(func.count())
        .select_from(KbDocument)
        .where(*filters)
    ).scalar_one()
    rows = session.execute(
        select(KbDocument, KbDocumentVersion)
        .join(
            KbDocumentVersion,
            (KbDocumentVersion.document_id == KbDocument.id)
            & (KbDocumentVersion.version_no == KbDocument.current_version_no),
        )
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
            deleted_at=document.deleted_at,
            created_at=version.created_at,
        )
        for document, version in rows
    ]
    return DocumentListResponse(kb_id=kb_id, total=total, items=items)


@router.delete("/documents/{document_id}", response_model=DocumentDeleted)
def delete_document(
    document_id: int, session: SessionDep, principal: PrincipalDep
) -> DocumentDeleted:
    """逻辑删除：同事务写 deleted_at 并递增 data_revision（实现文档 §5.3），
    随后登记重建与物理清理任务。"""
    document = session.get(KbDocument, document_id)
    if document is None:
        raise NotFound("文档不存在")
    require_kb_access(kb_id=document.kb_id, session=session, principal=principal)
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
