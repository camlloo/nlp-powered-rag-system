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
import json
from pathlib import PurePosixPath
from uuid import uuid4

from fastapi import APIRouter, File, Form, Header, Query, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy import delete, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.api.deps import PrincipalDep, SessionDep, SettingsDep, require_kb_access
from app.core.auth import Principal
from app.core.config import Settings
from app.core.errors import AppError, Conflict, Forbidden, NotFound, ValidationFailed
from app.db.models import (
    AppUser,
    IngestJob,
    KbChunk,
    KbCollection,
    KbDocument,
    KbDocumentVersion,
    OrgTag,
)
from app.db.spaces import get_or_create_main_space
from app.db.upload_guard import upload_idempotency, upload_object_guard
from app.parsers.base import check_size, detect_format
from app.schemas.api import (
    DocumentItem,
    DocumentPatchRequest,
    DocumentUploaded,
    GlobalDocumentListResponse,
)
from app.storage import MinioStorage, StorageError, build_object_key, object_uri

router = APIRouter()


def _hash_json(value: list) -> str:
    """避免拼接分隔符歧义，并保持 Unicode 原文不变。"""
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _lock_live_kb(session: Session, kb_id: int) -> None:
    """与知识库删除串行复核目标库；不存在或已删除即拒绝继续。"""
    row = session.execute(
        select(KbCollection.id, KbCollection.deleted_at)
        .where(KbCollection.id == kb_id)
        .with_for_update()
    ).one_or_none()
    if row is None or row[1] is not None:
        raise Conflict("知识库不存在或已删除，无法继续上传")


def _replacement_document_ids(session: Session, uri: str, kb_id: int) -> list[int]:
    """目标知识库内，按固定顺序锁定共用该 URI 的活文档（同库同名替换语义）。

    跨库共用 URI 的情况已在上传入口以 409 fail-closed 拒绝，此处不触碰其他库。"""
    return list(
        session.execute(
            select(KbDocument.id)
            .where(
                KbDocument.id.in_(
                    select(KbDocumentVersion.document_id).where(
                        KbDocumentVersion.storage_uri == uri
                    )
                ),
                KbDocument.deleted_at.is_(None),
                KbDocument.kb_id == kb_id,
            )
            .order_by(KbDocument.id)
            .with_for_update()
        ).scalars().all()
    )


def _foreign_kb_ids_using_uri(session: Session, uri: str, kb_id: int) -> list[int]:
    """仍持有该 URI 活文档的其他知识库 ID（升序）。

    对象 key 以（日期, 用户, 文件名）为粒度：同用户同日向不同库上传同名文件会
    指向同一 URI。若放任替换，会静默软删其他库的文档（历史数据丢失缺陷），
    因此上传入口对此 fail closed。"""
    return sorted(set(session.execute(
        select(KbDocument.kb_id)
        .where(
            KbDocument.id.in_(
                select(KbDocumentVersion.document_id).where(
                    KbDocumentVersion.storage_uri == uri
                )
            ),
            KbDocument.deleted_at.is_(None),
            KbDocument.kb_id != kb_id,
        )
    ).scalars().all()))


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
    """先持久阻断同键并使全部旧引用失效，再删、传、登记。

    writing 是永久屏障而非租约。任何远程请求异常、进程退出或最终提交
    不确定都不能自动重试或删除对象；需由运维确认远程请求已结束后处理。
    """
    if session.get_bind().dialect.name != "postgresql":
        raise AppError("上传需要 PostgreSQL 行锁", status_code=503, retryable=False)
    if session.scalar(text("SHOW transaction_isolation")) != "read committed":
        raise AppError("上传需要 read committed 事务隔离级别", status_code=503, retryable=False)
    if visibility not in ("private", "public"):
        raise ValidationFailed("visibility 仅支持 private / public")
    if idempotency_key is not None and (not idempotency_key or len(idempotency_key) > 512):
        raise ValidationFailed("Idempotency-Key 长度须为 1 至 512 个字符")
    if len(route) > 200:
        raise ValidationFailed("上传路由无效")
    try:
        key = build_object_key(principal.user_id, filename)
        uri = object_uri(settings.minio_bucket, key)
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from None
    title = PurePosixPath(filename).stem
    if len(title) > 300:
        raise ValidationFailed("文件名不含扩展名部分超过 300 个字符")

    kb_id = kb.id
    digest = hashlib.sha256(payload).digest()
    request_digest = _hash_json(
        [principal.tenant_id, principal.user_id, kb_id, org_tag_id, visibility,
         filename, content_type, fmt, digest.hex()]
    )
    identity_hash = (
        _hash_json([principal.user_id, route, idempotency_key])
        if idempotency_key is not None else None
    )
    uri_hash = hashlib.sha256(uri.encode("utf-8")).hexdigest()
    token = uuid4().hex

    try:
        if identity_hash is not None:
            session.execute(
                insert(upload_idempotency).values(
                    identity_hash=identity_hash, user_id=principal.user_id, route=route,
                    request_key=idempotency_key, request_digest=request_digest,
                    state="pending", token=token,
                ).on_conflict_do_nothing(index_elements=["identity_hash"])
            )
            idem = session.execute(
                select(upload_idempotency).where(
                    upload_idempotency.c.identity_hash == identity_hash
                ).with_for_update()
            ).mappings().one()
            if (idem["user_id"], idem["route"], idem["request_key"]) != (
                principal.user_id, route, idempotency_key
            ) or idem["request_digest"] != request_digest:
                raise Conflict("相同 Idempotency-Key 但请求内容不同")
            if idem["state"] == "completed":
                body = DocumentUploaded(
                    job_id=idem["job_id"], document_id=idem["document_id"],
                    version_no=idem["version_no"], kb_id=idem["kb_id"],
                )
                session.rollback()
                return JSONResponse(
                    status_code=202, content=body.model_dump(),
                    headers={"Idempotent-Replay": "true"},
                )
            if idem["token"] != token:
                raise Conflict("该幂等请求尚未完成或结果不确定，需人工核查")

        # 构造客户端不发网络请求；配置失败则回滚未提交的幂等行。
        try:
            storage = MinioStorage(settings)
        except StorageError:
            raise AppError("对象存储配置不可用", status_code=503, retryable=False) from None
        # 若 SDK 隐藏重试超时的 DELETE，第一次请求可能迟到并删除后续 PUT。
        # 未明确保证禁用全部对象写入重试时必须 fail closed，不触碰旧记录或对象。
        if getattr(storage, "safe_mutation_no_retry", False) is not True:
            raise AppError(
                "对象存储未保证禁用写入自动重试，暂不支持安全上传",
                status_code=503, retryable=False,
            )

        session.execute(
            insert(upload_object_guard).values(
                uri_hash=uri_hash, uri=uri, state="ready", token=None
            ).on_conflict_do_nothing(index_elements=["uri_hash"])
        )
        guard = session.execute(
            select(upload_object_guard).where(
                upload_object_guard.c.uri_hash == uri_hash
            ).with_for_update()
        ).mappings().one()
        if guard["uri"] != uri:
            raise Conflict("对象标识冲突，不能上传")
        if guard["state"] != "ready":
            raise Conflict("同名对象正在处理或结果不确定，需人工核查")
        # 持住屏障后复核目标知识库：删除端点同样原子更新，二者串行。
        _lock_live_kb(session, kb_id)
        # 同 URI 若被其他知识库的活文档引用：fail closed 拒绝，不写屏障、不动对象、
        # 不软删别的库文档（跨库同名同日上传共享 URI 的历史数据丢失缺陷）。
        foreign = _foreign_kb_ids_using_uri(session, uri, kb_id)
        if foreign:
            raise Conflict(
                f"同名对象已被其他知识库 {foreign} 的文档引用：请改用不同文件名，或先删除对应文档"
            )
        session.execute(
            update(upload_object_guard).where(
                upload_object_guard.c.uri_hash == uri_hash
            ).values(state="writing", token=token)
        )

        # 同库内替换语义保留：共用此 URI 的本库旧版本会被改写（固定 ID 顺序加锁）。
        stale_ids = _replacement_document_ids(session, uri, kb_id)
        affected = []
        if stale_ids:
            affected = session.execute(
                update(KbDocument)
                .where(KbDocument.id.in_(stale_ids), KbDocument.deleted_at.is_(None))
                .values(deleted_at=func.now()).returning(KbDocument.kb_id)
                .execution_options(synchronize_session=False)
            ).scalars().all()
        for affected_kb_id in sorted(set(affected)):
            session.execute(
                update(KbCollection).where(KbCollection.id == affected_kb_id)
                .values(data_revision=KbCollection.data_revision + 1)
                .execution_options(synchronize_session=False)
            )
            session.add(IngestJob(
                kb_id=affected_kb_id, job_type="reindex",
                payload_json={"reason": "object_replaced"},
            ))
        # 此提交必须成功返回，才能操作对象存储。
        session.commit()
    except AppError:
        session.rollback()
        raise
    except Exception:
        session.rollback()
        raise AppError(
            "无法确认上传屏障提交，未发起对象写入，请核查后重试",
            status_code=503, retryable=False,
        ) from None

    try:
        # DELETE 对不存在的 key 也有效，确保不依赖可能过时的存在性检查。
        storage.delete(uri)
        returned_uri = storage.put(
            key, payload, content_type=content_type or "application/octet-stream"
        )
        if returned_uri != uri:
            raise ValueError("对象 URI 不一致")
    except Exception:
        # 请求可能仍在服务端运行：不补偿、不自动释放，也不尝试再次 DELETE。
        raise AppError(
            "对象存储操作失败或结果不确定；同名对象已阻断，需人工核查",
            status_code=503, retryable=False,
        ) from None

    try:
        # 与准备事务按相同顺序持锁，避免重试请求与完成事务反向死锁。
        if identity_hash is not None:
            idem = session.execute(
                select(upload_idempotency).where(
                    upload_idempotency.c.identity_hash == identity_hash
                ).with_for_update()
            ).mappings().one()
            if idem["state"] != "pending" or idem["token"] != token:
                raise Conflict("上传幂等状态已改变，无法登记")
        guard = session.execute(
            select(upload_object_guard).where(
                upload_object_guard.c.uri_hash == uri_hash
            ).with_for_update()
        ).mappings().one()
        if guard["uri"] != uri or guard["state"] != "writing" or guard["token"] != token:
            raise Conflict("对象写入屏障已改变，无法登记")
        # PUT 成功与目标知识库删除并发时不能登记。此分支状态完全已知：对象已在桶中
        # 但无文档引用，属可重复触发的同键先删后传语义；释放屏障供运维/后续同名上传清理。
        try:
            _lock_live_kb(session, kb_id)
        except Conflict:
            session.execute(
                update(upload_object_guard)
                .where(
                    upload_object_guard.c.uri_hash == uri_hash,
                    upload_object_guard.c.state == "writing",
                    upload_object_guard.c.token == token,
                )
                .values(state="ready", token=None)
            )
            if identity_hash is not None:
                session.execute(
                    delete(upload_idempotency).where(
                        upload_idempotency.c.identity_hash == identity_hash
                    )
                )
            session.commit()
            raise AppError(
                "知识库已在上传期间删除：对象已写入但未登记，同键后续上传将先删除它",
                status_code=409, retryable=False,
            ) from None
        document = KbDocument(
            kb_id=kb_id, title=title, uploader_user_id=principal.user_id,
            visibility=visibility, org_tag_id=org_tag_id,
        )
        session.add(document)
        session.flush()
        version = KbDocumentVersion(
            kb_id=kb_id, document_id=document.id, version_no=1, title=title,
            file_sha256=digest, storage_uri=uri, parse_status="pending",
        )
        session.add(version)
        session.flush()
        job = IngestJob(
            kb_id=kb_id, doc_version_id=version.id, job_type="ingest_file",
            payload_json={
                "filename": filename, "content_type": content_type, "format": fmt,
                "uploader_user_id": principal.user_id,
            },
        )
        session.add(job)
        session.flush()
        session.execute(
            update(KbCollection).where(KbCollection.id == kb_id)
            .values(data_revision=KbCollection.data_revision + 1)
            .execution_options(synchronize_session=False)
        )
        result = session.execute(
            update(upload_object_guard).where(
                upload_object_guard.c.uri_hash == uri_hash,
                upload_object_guard.c.state == "writing", upload_object_guard.c.token == token,
            ).values(state="ready", token=None)
        )
        if result.rowcount != 1:
            raise Conflict("对象写入屏障已改变，无法登记")
        body = DocumentUploaded(
            job_id=job.id, document_id=document.id, version_no=version.version_no, kb_id=kb_id
        )
        if identity_hash is not None:
            result = session.execute(
                update(upload_idempotency).where(
                    upload_idempotency.c.identity_hash == identity_hash,
                    upload_idempotency.c.state == "pending", upload_idempotency.c.token == token,
                ).values(
                    state="completed", token=None, job_id=job.id, document_id=document.id,
                    version_no=version.version_no, kb_id=kb_id,
                )
            )
            if result.rowcount != 1:
                raise Conflict("上传幂等状态已改变，无法登记")
        session.commit()
        return body
    except AppError:
        session.rollback()
        raise
    except Exception:
        session.rollback()
        raise AppError(
            "对象已上传，但数据库登记结果不确定；请勿重试，需人工核查",
            status_code=503, retryable=False,
        ) from None


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
    payload = await file.read(settings.max_upload_bytes + 1)
    check_size(len(payload), settings)  # 413
    fmt = detect_format(filename, file.content_type, payload)  # 422
    org_tag_id = _resolve_org_scope(session, principal, scope)
    kb = get_or_create_main_space(session)
    return await run_in_threadpool(
        create_document_record,
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
    filters = [
        KbDocument.kb_id.in_(
            select(KbCollection.id).where(
                KbCollection.tenant_id == principal.tenant_id,
                KbCollection.deleted_at.is_(None),
            )
        )
    ]
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
        select(KbDocument, KbDocumentVersion, OrgTag.name, AppUser.username)
        .join(
            KbDocumentVersion,
            (KbDocumentVersion.document_id == KbDocument.id)
            & (KbDocumentVersion.version_no == KbDocument.current_version_no),
        )
        .outerjoin(OrgTag, OrgTag.id == KbDocument.org_tag_id)
        .outerjoin(AppUser, AppUser.id == KbDocument.uploader_user_id)
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
            uploader_username=uploader_username,
            deleted_at=document.deleted_at,
            created_at=version.created_at,
        )
        for document, version, org_name, uploader_username in rows
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
    document: KbDocument, version: KbDocumentVersion, org_name: str | None,
    uploader_username: str | None = None,
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
        uploader_username=uploader_username,
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
    # 授权变化：原子递增 acl_revision，避免丢失并发授权变更。
    session.execute(
        update(KbCollection)
        .where(KbCollection.id == document.kb_id)
        .values(acl_revision=KbCollection.acl_revision + 1)
    )
    session.commit()

    row = _load_document_row(session, document_id)
    assert row is not None
    uploader_username = session.scalar(
        select(AppUser.username).where(AppUser.id == document.uploader_user_id)
    )
    return _document_item(*row, uploader_username)
