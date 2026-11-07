"""按保留策略物理清理旧快照、协议内过期对象与遗留记录。

固定 key 的所有写删必须遵循 upload_object_guard 协议；清理与上传共用
guard → KB → document 锁序和 ready→writing→ready 屏障状态机：

- 对象持有 ready 屏障且无任何存活引用时，先提交 writing 屏障，再发远端
  DELETE；只有 DELETE 确认完成、版本元数据删除与屏障释放在同一事务提交，
  才算清理成功。objects_deleted 计的是确认成功的 DELETE 请求数。
- 缺行 = 迁移前的遗留对象：协议无法证明过去没有未决写入，因此保守保留
  对象与版本元数据，计入 versions_retained 并告警；不删除，也不抛错
  （重试永远不会改变结论），由运维核查后处置。
- writing 屏障（他人正在写入或结果不确定）、URI 冲突、远端 DELETE 或任一
  提交结果不确定：fail-closed 抛错，保留 writing 屏障、版本元数据与对象，
  绝不自动解锁、补偿或重发远端请求；等待人工核查，禁止报告成功。
- 整库删除采用原子预检：任一版本缺屏障、屏障处于 writing、或仍有未完成
  索引/清理引用任务时，整个知识库内容（快照、块、版本、对象、文档）
  原样保留；预检通过才统一物理清除。
- 知识库父行（tombstone）及其任务记录始终保留：删除正在执行本函数的
  cleanup job 会让 worker 误报成功，父行也是审计锚点；由运维另行处置。
- building 快照可能属于失租 worker，保守保留并抛错等待核查。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
from uuid import uuid4

from sqlalchemy import delete, select, text, update

from app.core.config import Settings
from app.core.errors import AppError
from app.db.models import (
    IngestJob,
    KbChunk,
    KbCollection,
    KbDocument,
    KbDocumentVersion,
    KbEmbedding,
    KbIndex,
)
from app.db.upload_guard import upload_object_guard
from app.storage import MinioStorage, parse_object_uri

logger = logging.getLogger(__name__)

# _purge_version 的结果
PURGED = "purged"  # 远端 DELETE 确认 + 元数据删除 + 屏障释放
METADATA_ONLY = "metadata_only"  # 同 URI 仍有其他版本：只删元数据，保留对象
LEGACY = "legacy"  # 缺屏障：遗留对象，保守保留，无变更
SKIPPED = "skipped"  # 目标已变化：无事可做


class CleanupDeferred(AppError):
    """目标状态不确定或暂不可安全清理，不能报告清理成功。"""

    status_code = 503
    code = "cleanup_deferred"
    retryable = True


def _require_cleanup_database(session) -> None:
    # 与上传相同：锁等待后查询必须看到最新已提交的引用。
    if session.get_bind().dialect.name != "postgresql":
        raise CleanupDeferred("清理需要 PostgreSQL 行锁", retryable=False)
    if session.scalar(text("SHOW transaction_isolation")) != "read committed":
        raise CleanupDeferred("清理需要 read committed 事务隔离级别", retryable=False)


def _has_rows(session, statement) -> bool:
    return session.execute(statement.limit(1)).first() is not None


def _active_work(session, kb_id: int) -> bool:
    return _has_rows(
        session,
        select(IngestJob.id).where(
            IngestJob.kb_id == kb_id,
            IngestJob.job_type != "cleanup",
            IngestJob.status.in_(("queued", "running")),
        ),
    )


def _pending_version_jobs(session, version_ids: list[int]) -> bool:
    if not version_ids:
        return False
    return _has_rows(
        session,
        select(IngestJob.id).where(
            IngestJob.doc_version_id.in_(version_ids),
            IngestJob.status.in_(("queued", "running")),
        ),
    )


def _snapshot_references(session, kb_id: int, version_id: int) -> bool:
    if _has_rows(session, select(KbChunk.id).where(KbChunk.doc_version_id == version_id)):
        return True
    # 零块文件或尚未写块的快照同样可能在 manifest 中保留版本。
    for manifest in session.execute(
        select(KbIndex.manifest_json).where(KbIndex.kb_id == kb_id)
    ).scalars():
        if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
            return True  # 旧格式或损坏的清单不能证明没有引用
        files = manifest["files"]
        if any(type(item) is not int for item in files) or version_id in files:
            return True
    return False


def _eligible(document, kb, cutoff: dt.datetime) -> bool:
    return (
        kb is not None
        and document is not None
        and document.kb_id == kb.id
        and (
            (kb.deleted_at is not None and kb.deleted_at < cutoff)
            or (document.deleted_at is not None and document.deleted_at < cutoff)
        )
    )


def _lock_guard(session, uri: str):
    """按 uri_hash 获取屏障行锁，返回 (uri_hash, guard 行或 None)。

    None 表示缺行 = 迁移前遗留，调用方必须保守保留、不得删除对象；
    任何情况下都不为历史 URI 凭空补建 ready，以免误判未决写入已结束。
    """
    uri_hash = hashlib.sha256(uri.encode("utf-8")).hexdigest()
    guard = session.execute(
        select(upload_object_guard).where(
            upload_object_guard.c.uri_hash == uri_hash
        ).with_for_update()
    ).mappings().one_or_none()
    if guard is None:
        return uri_hash, None
    if guard["uri"] != uri:
        raise CleanupDeferred("对象屏障 URI 与版本记录冲突，不能清理", retryable=False)
    if guard["state"] != "ready" or guard["token"] is not None:
        raise CleanupDeferred("同名对象正在写入或屏障结果不确定，等待屏障解除后再试")
    return uri_hash, guard


def _lock_target(session, version_id: int, document_id: int, kb_id: int, cutoff):
    # 上传的准备阶段采用 guard → KB → 文档顺序，清理保持相同顺序。
    kb = session.execute(
        select(KbCollection).where(KbCollection.id == kb_id).with_for_update()
    ).scalar_one_or_none()
    document = session.execute(
        select(KbDocument).where(KbDocument.id == document_id).with_for_update()
    ).scalar_one_or_none()
    version = session.execute(
        select(KbDocumentVersion).where(KbDocumentVersion.id == version_id)
        .with_for_update().execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if not _eligible(document, kb, cutoff) or version is None:
        return None
    if version.kb_id != kb_id or version.document_id != document_id:
        raise CleanupDeferred("文档版本归属已改变，保留清理元信息")
    # 整库过期时也逐一使文档失效。新建快照只筛 document.deleted_at，
    # 必须在释放 KB 锁前使其不可再次进入清单。
    if document.deleted_at is None and kb.deleted_at is not None and kb.deleted_at < cutoff:
        document.deleted_at = kb.deleted_at
    return version


def _delete_version_metadata(session, version_id: int) -> None:
    # 活跃任务不能解引用；已结束的任务保留，但移除可空外键。
    session.execute(
        update(IngestJob).where(IngestJob.doc_version_id == version_id)
        .values(doc_version_id=None)
    )
    session.execute(delete(KbDocumentVersion).where(KbDocumentVersion.id == version_id))


def _purge_version(session_factory, settings: Settings, *, version_id: int, cutoff) -> str:
    """清理一个版本，返回 PURGED/METADATA_ONLY/LEGACY/SKIPPED；不确定时抛错。"""
    with session_factory() as session:
        _require_cleanup_database(session)
        original = session.get(KbDocumentVersion, version_id)
        if original is None:
            return SKIPPED
        uri, document_id, kb_id = original.storage_uri, original.document_id, original.kb_id
        uri_hash, guard = _lock_guard(session, uri)
        if guard is None:
            # 迁移前遗留：过去写入历史不可证明，保守保留，不删除、不变更。
            return LEGACY
        version = _lock_target(session, version_id, document_id, kb_id, cutoff)
        if version is None:
            return SKIPPED
        if version.storage_uri != uri:
            return SKIPPED  # 已被替换上传改写，旧引用自然作废，新对象不得删除
        if _active_work(session, kb_id):
            raise CleanupDeferred("知识库仍有未完成的索引任务")
        if _snapshot_references(session, kb_id, version_id):
            raise CleanupDeferred("版本仍被保留快照引用，等待旧快照过期")
        if _has_rows(
            session,
            select(IngestJob.id).where(
                IngestJob.doc_version_id == version_id,
                IngestJob.status.in_(("queued", "running")),
            ),
        ):
            raise CleanupDeferred("版本仍被未完成任务引用")
        if _has_rows(
            session,
            select(KbDocumentVersion.id).where(
                KbDocumentVersion.storage_uri == uri, KbDocumentVersion.id != version_id
            ),
        ):
            # 其他版本（可跨知识库）仍引用同一对象：只删本版本元数据。
            # 同 URI 的并发清理受同一 guard 行锁串行化。
            _delete_version_metadata(session, version_id)
            session.commit()
            return METADATA_ONLY

        try:
            parse_object_uri(uri, bucket=settings.minio_bucket)
            storage = MinioStorage(settings)
            if getattr(storage, "safe_mutation_no_retry", False) is not True:
                raise ValueError("unsafe storage transport")
        except CleanupDeferred:
            raise
        except Exception:
            raise CleanupDeferred(
                "对象 URI 或存储配置不支持安全清理，修复配置后可重试"
            ) from None

        token = uuid4().hex
        session.execute(
            update(upload_object_guard).where(upload_object_guard.c.uri_hash == uri_hash)
            .values(state="writing", token=token)
        )
        try:
            # 如果提交未确认，绝不发起远程操作；提交即使已成功也会留下屏障。
            session.commit()
        except Exception:
            raise CleanupDeferred(
                "无法确认清理屏障提交；未删除对象，需核查", retryable=False
            ) from None

    try:
        storage.delete(uri)
    except Exception:
        # DELETE 可能仍在服务端执行，不能解锁、补偿或自动重试。
        raise CleanupDeferred(
            "对象删除结果不确定；保留屏障和版本，需人工核查", retryable=False
        ) from None

    with session_factory() as session:
        _require_cleanup_database(session)
        guard = session.execute(
            select(upload_object_guard).where(
                upload_object_guard.c.uri_hash == uri_hash
            ).with_for_update()
        ).mappings().one()
        if guard["uri"] != uri or guard["state"] != "writing" or guard["token"] != token:
            raise CleanupDeferred("清理屏障已改变；保留版本并核查", retryable=False)
        version = _lock_target(session, version_id, document_id, kb_id, cutoff)
        if version is None or version.storage_uri != uri:
            raise CleanupDeferred("删除后版本状态已改变；保留屏障并核查", retryable=False)
        if (
            _snapshot_references(session, kb_id, version_id)
            or _has_rows(
                session,
                select(IngestJob.id).where(
                    IngestJob.doc_version_id == version_id,
                    IngestJob.status.in_(("queued", "running")),
                ),
            )
            or _has_rows(
                session,
                select(KbDocumentVersion.id).where(
                    KbDocumentVersion.storage_uri == uri, KbDocumentVersion.id != version_id
                ),
            )
        ):
            raise CleanupDeferred("删除后发现新引用；保留屏障及版本并核查", retryable=False)
        _delete_version_metadata(session, version_id)
        result = session.execute(
            update(upload_object_guard).where(
                upload_object_guard.c.uri_hash == uri_hash,
                upload_object_guard.c.state == "writing",
                upload_object_guard.c.token == token,
            ).values(state="ready", token=None)
        )
        if result.rowcount != 1:
            raise CleanupDeferred("清理屏障所有权已改变；保留版本并核查", retryable=False)
        try:
            session.commit()
        except Exception:
            # 事务要么一起完成，要么保留 writing 与版本；不能在此重发 DELETE。
            raise CleanupDeferred("清理提交结果不确定；需核查后重试", retryable=False) from None
    return PURGED


def _whole_kb_preflight(session_factory, kb_id: int, cutoff) -> tuple[str, list[int], list[int]]:
    """整库删除的原子预检，返回 (mode, version_ids, legacy_ids)。

    mode 为 "legacy"（存在缺屏障版本，整库内容必须原样保留）或
    "purge"（全部版本都有 ready 屏障且无活动引用，可统一清除）。
    屏障处于 writing、URI 冲突或仍有未完成任务时抛错（不确定，勿清理）。
    """
    with session_factory() as session:
        _require_cleanup_database(session)
        rows = session.execute(
            select(KbDocumentVersion.id, KbDocumentVersion.storage_uri)
            .where(KbDocumentVersion.kb_id == kb_id)
            .order_by(KbDocumentVersion.id)
        ).all()
        version_ids = [row[0] for row in rows]
        legacy_ids = []
        for version_id, uri in rows:
            _, guard = _lock_guard(session, uri)
            if guard is None:
                legacy_ids.append(version_id)
        if _active_work(session, kb_id) or _pending_version_jobs(session, version_ids):
            raise CleanupDeferred("知识库仍有未完成的索引或清理引用任务")
        kb = session.execute(
            select(KbCollection).where(KbCollection.id == kb_id).with_for_update()
        ).scalar_one_or_none()
        if kb is None or kb.deleted_at is None or kb.deleted_at >= cutoff:
            raise CleanupDeferred("知识库删除状态在预检期间改变，重新调度清理")
        return ("legacy" if legacy_ids else "purge"), version_ids, legacy_ids


def _cleanup_snapshots(
    session_factory, kb_id: int, cutoff, counts: dict, *, whole: bool
) -> bool:
    """清除快照及块向量；返回是否存在未完成的索引任务。

    whole=True 用于整库清除：连同活跃快照一起删除，并把全部文档失效；
    调用方必须先通过 _whole_kb_preflight。
    """
    with session_factory() as session:
        _require_cleanup_database(session)
        kb = session.execute(
            select(KbCollection).where(KbCollection.id == kb_id).with_for_update()
        ).scalar_one_or_none()
        if kb is None:
            return False
        if _active_work(session, kb_id):
            return True
        if whole:
            # 快照构建按文档 deleted_at 固定清单，即使 KB 已删除也会查活文档。
            # 清理索引前先使整库文档失效，避免构建在删除旧快照与版本之间
            # 固定到旧版本；该变更与索引删除在同一事务提交。
            session.execute(
                update(KbDocument).where(
                    KbDocument.kb_id == kb_id, KbDocument.deleted_at.is_(None)
                ).values(deleted_at=kb.deleted_at)
                .execution_options(synchronize_session=False)
            )
        eligible = select(KbIndex.id).where(KbIndex.kb_id == kb_id)
        if not whole:
            eligible = eligible.where(KbIndex.created_at < cutoff)
            if kb.active_index_id is not None:
                eligible = eligible.where(KbIndex.id != kb.active_index_id)
        # building 可能仍由失去租约的 worker 使用；不能仅凭时间删除。
        counts["indexes_retained"] += len(list(session.execute(
            eligible.where(KbIndex.status == "building")
        ).scalars()))
        # 锁住目标行，同时持有 KB 锁，避免并发发布把快照切换成 active
        # 后仍由旧判断删除；活跃构建快照单独保留。
        index_ids = list(session.execute(
            eligible.where(KbIndex.status != "building").with_for_update()
        ).scalars())
        chunk_ids: list[int] = []
        if index_ids:
            chunk_ids = list(session.execute(
                select(KbChunk.id).where(KbChunk.index_id.in_(index_ids))
            ).scalars())
            if chunk_ids:
                session.execute(delete(KbEmbedding).where(KbEmbedding.chunk_id.in_(chunk_ids)))
                session.execute(delete(KbChunk).where(KbChunk.id.in_(chunk_ids)))
            session.execute(delete(KbIndex).where(KbIndex.id.in_(index_ids)))
            if kb.active_index_id in index_ids:
                kb.active_index_id = None
        if whole or index_ids:
            session.commit()
            counts["indexes_removed"] += len(index_ids)
            counts["chunks_removed"] += len(chunk_ids)
        return False


def _purge_empty_document(session_factory, document_id: int, kb_id: int, cutoff) -> bool:
    with session_factory() as session:
        _require_cleanup_database(session)
        kb = session.execute(
            select(KbCollection).where(KbCollection.id == kb_id).with_for_update()
        ).scalar_one_or_none()
        document = session.execute(
            select(KbDocument).where(KbDocument.id == document_id).with_for_update()
        ).scalar_one_or_none()
        if not _eligible(document, kb, cutoff):
            return False
        if _has_rows(
            session, select(KbDocumentVersion.id).where(
                KbDocumentVersion.document_id == document_id
            )
        ):
            return False
        session.execute(delete(KbDocument).where(KbDocument.id == document_id))
        session.commit()
        return True


def _record_version_result(counts: dict, status: str) -> None:
    if status == PURGED:
        counts["objects_deleted"] += 1
    elif status == LEGACY:
        counts["versions_retained"] += 1


def _raise_deferred(deferred: list[CleanupDeferred]) -> None:
    if not deferred:
        return
    # 多个问题并存时，只要有一个需要人工核查就不能标记为可自动重试。
    problem = next((item for item in deferred if not item.retryable), deferred[0])
    raise CleanupDeferred(f"清理未完成：{problem.message}", retryable=problem.retryable)


def cleanup_kb_scope(
    session_factory, settings: Settings, *, kb_id: int, now: dt.datetime | None = None
) -> dict:
    """清理单个知识库；不确定或受阻目标抛 CleanupDeferred。

    遗留缺屏障对象只计数不抛错（重试无法改变结论）；整库删除在预检
    不通过时原样保留全部库内容。知识库父行永远保留为 tombstone，
    由运维另行处置，因此整库清理后 kb_retained 恒为 1。
    """
    now = now or dt.datetime.now(dt.UTC)
    cutoff = now - dt.timedelta(hours=settings.cleanup_retention_hours)
    counts = {
        "indexes_removed": 0, "indexes_retained": 0, "chunks_removed": 0,
        "documents_purged": 0, "documents_retained": 0,
        "versions_retained": 0, "objects_deleted": 0,
    }
    with session_factory() as session:
        _require_cleanup_database(session)
        kb = session.get(KbCollection, kb_id)
        if kb is None:
            return counts
        expired_kb = kb.deleted_at is not None and kb.deleted_at < cutoff

    if expired_kb:
        mode, version_ids, legacy_ids = _whole_kb_preflight(session_factory, kb_id, cutoff)
        if mode == "legacy":
            counts["kb_retained"] = 1
            counts["versions_retained"] = len(legacy_ids)
            logger.warning(
                "cleanup kb=%s: %s 个版本缺少持久屏障（迁移前遗留），整库内容原样保留",
                kb_id, len(legacy_ids),
            )
            return counts
        deferred: list[CleanupDeferred] = []
        if _cleanup_snapshots(session_factory, kb_id, cutoff, counts, whole=True):
            deferred.append(CleanupDeferred("知识库仍有未完成的索引任务"))
        if counts["indexes_retained"]:
            deferred.append(CleanupDeferred(
                "存在构建中快照，等待核查或重新调度清理", retryable=False
            ))
        _raise_deferred(deferred)
        for version_id in version_ids:
            status = _purge_version(session_factory, settings, version_id=version_id, cutoff=cutoff)
            _record_version_result(counts, status)
        with session_factory() as session:
            _require_cleanup_database(session)
            document_ids = list(session.execute(
                select(KbDocument.id).where(KbDocument.kb_id == kb_id).order_by(KbDocument.id)
            ).scalars())
        for document_id in document_ids:
            if _purge_empty_document(session_factory, document_id, kb_id, cutoff):
                counts["documents_purged"] += 1
            else:
                counts["documents_retained"] += 1
        counts["kb_retained"] = 1  # tombstone：父行按设计保留，运维另行处置
        if any(counts.values()):
            logger.info("cleanup kb=%s: %s", kb_id, counts)
        return counts

    deferred = []
    active_work = _cleanup_snapshots(session_factory, kb_id, cutoff, counts, whole=False)
    with session_factory() as session:
        _require_cleanup_database(session)
        kb = session.get(KbCollection, kb_id)
        if kb is None:
            return counts
        document_ids = list(session.execute(
            select(KbDocument.id).where(
                KbDocument.kb_id == kb_id,
                KbDocument.deleted_at.is_not(None),
                KbDocument.deleted_at < cutoff,
            ).order_by(KbDocument.id)
        ).scalars())
        retention_pending = (
            (kb.deleted_at is not None and kb.deleted_at >= cutoff)
            or _has_rows(
                session,
                select(KbDocument.id).where(
                    KbDocument.kb_id == kb_id,
                    KbDocument.deleted_at.is_not(None),
                    KbDocument.deleted_at >= cutoff,
                ),
            )
        )

    if active_work:
        deferred.append(CleanupDeferred("知识库仍有未完成的索引任务"))
    if counts["indexes_retained"]:
        deferred.append(CleanupDeferred("存在构建中快照，等待核查或重新调度清理", retryable=False))
    if retention_pending:
        deferred.append(CleanupDeferred("仍有软删除目标在保留期内"))
    for document_id in document_ids:
        with session_factory() as session:
            _require_cleanup_database(session)
            version_ids = list(session.execute(
                select(KbDocumentVersion.id).where(
                    KbDocumentVersion.document_id == document_id
                ).order_by(KbDocumentVersion.id)
            ).scalars())
        for version_id in version_ids:
            try:
                status = _purge_version(
                    session_factory, settings, version_id=version_id, cutoff=cutoff
                )
            except CleanupDeferred as exc:
                logger.warning("cleanup kb=%s version=%s: %s", kb_id, version_id, exc.message)
                deferred.append(exc)
            else:
                _record_version_result(counts, status)
        if _purge_empty_document(session_factory, document_id, kb_id, cutoff):
            counts["documents_purged"] += 1
        else:
            counts["documents_retained"] += 1

    if any(counts.values()):
        logger.info("cleanup kb=%s: %s", kb_id, counts)
    if counts["versions_retained"]:
        logger.warning(
            "cleanup kb=%s: 保留 %s 个缺少持久屏障的遗留版本（不删除对象）",
            kb_id, counts["versions_retained"],
        )
    _raise_deferred(deferred)
    return counts
