"""物理清理：软删除文档与旧快照按保留策略物理清除（实现文档 §5.3）。

- 范围：非当前活跃快照（superseded / building 遗留 / 过期 ready）的块与向量、
  软删除文档的原始文件与版本记录；
- 保留策略：created_at / deleted_at 早于 cleanup_retention_hours；
- 顺序：按子表到父表（kb_embedding -> kb_chunk -> kb_index -> kb_document_version
  -> kb_document），无 ON DELETE CASCADE，全程显式删除并返回计数；
- 前提保护：文档的版本仍被任何快照块引用时不物理清除（等旧快照清完）；
  活跃快照永不清理。
"""

from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import delete, select, text

from app.core.config import Settings
from app.db.models import (
    IngestJob,
    KbChunk,
    KbCollection,
    KbDocument,
    KbDocumentVersion,
    KbEmbedding,
    KbIndex,
)
from app.parsers.intake import delete_upload

logger = logging.getLogger(__name__)


def cleanup_kb_scope(
    session_factory, settings: Settings, *, kb_id: int, now: dt.datetime | None = None
) -> dict:
    """清理单个知识库的过期数据；返回各对象的删除计数。"""
    now = now or dt.datetime.now(dt.UTC)
    cutoff = now - dt.timedelta(hours=settings.cleanup_retention_hours)
    counts = {"indexes_removed": 0, "chunks_removed": 0, "documents_purged": 0}

    with session_factory() as session:
        kb = session.get(KbCollection, kb_id)
        if kb is None:
            return counts

        # 0) 整库软删除且过保留期：文件、向量、块、快照、版本、文档、成员、集合全部物理清除
        if kb.deleted_at is not None and kb.deleted_at < cutoff:
            versions = session.execute(
                select(KbDocumentVersion).where(KbDocumentVersion.kb_id == kb_id)
            ).scalars().all()
            for version in versions:
                try:
                    delete_upload(version.storage_uri, settings)
                except (ValueError, OSError) as exc:
                    logger.warning("清理文件失败 uri=%s: %s", version.storage_uri, exc)
            session.execute(
                delete(KbEmbedding).where(
                    KbEmbedding.chunk_id.in_(select(KbChunk.id).where(KbChunk.kb_id == kb_id))
                )
            )
            session.execute(delete(KbChunk).where(KbChunk.kb_id == kb_id))
            session.execute(delete(KbIndex).where(KbIndex.kb_id == kb_id))
            session.execute(
                delete(KbDocumentVersion).where(KbDocumentVersion.kb_id == kb_id)
            )
            session.execute(delete(KbDocument).where(KbDocument.kb_id == kb_id))
            session.execute(delete(IngestJob).where(IngestJob.kb_id == kb_id))
            session.execute(
                text("DELETE FROM kb_member WHERE kb_id = :k"), {"k": kb_id}
            )
            session.execute(delete(KbCollection).where(KbCollection.id == kb_id))
            session.commit()
            counts["kb_purged"] = 1
            logger.info("cleanup: 知识库 %s 已整库物理清除", kb_id)
            return counts

        # 1) 非活跃且过期的快照（superseded / building 遗留 / 旧 ready）
        stale_index_ids = list(
            session.execute(
                select(KbIndex.id).where(
                    KbIndex.kb_id == kb_id,
                    KbIndex.id != kb.active_index_id,
                    KbIndex.created_at < cutoff,
                )
            ).scalars()
        )
        if stale_index_ids:
            chunk_ids = list(
                session.execute(
                    select(KbChunk.id).where(KbChunk.index_id.in_(stale_index_ids))
                ).scalars()
            )
            if chunk_ids:
                session.execute(
                    delete(KbEmbedding).where(KbEmbedding.chunk_id.in_(chunk_ids))
                )
                counts["chunks_removed"] = len(chunk_ids)
            session.execute(delete(KbChunk).where(KbChunk.index_id.in_(stale_index_ids)))
            session.execute(delete(KbIndex).where(KbIndex.id.in_(stale_index_ids)))
            counts["indexes_removed"] = len(stale_index_ids)

        # 2) 软删除且过保留期的文档：版本不再被任何块引用才物理清除
        purged_docs = session.execute(
            select(KbDocument).where(
                KbDocument.kb_id == kb_id,
                KbDocument.deleted_at.is_not(None),
                KbDocument.deleted_at < cutoff,
            )
        ).scalars().all()
        for document in purged_docs:
            versions = session.execute(
                select(KbDocumentVersion).where(
                    KbDocumentVersion.document_id == document.id
                )
            ).scalars().all()
            version_ids = [v.id for v in versions]
            referenced = 0
            if version_ids:
                referenced = session.execute(
                    select(KbChunk.id).where(KbChunk.doc_version_id.in_(version_ids)).limit(1)
                ).scalar_one_or_none()
            if referenced is not None:
                continue  # 仍被快照引用：等下一轮清理
            for version in versions:
                try:
                    delete_upload(version.storage_uri, settings)
                except (ValueError, OSError) as exc:
                    logger.warning("清理文件失败 uri=%s: %s", version.storage_uri, exc)
                session.execute(
                    delete(KbDocumentVersion).where(KbDocumentVersion.id == version.id)
                )
            session.execute(delete(KbDocument).where(KbDocument.id == document.id))
            counts["documents_purged"] += 1

        session.commit()
    if any(counts.values()):
        logger.info("cleanup kb=%s: %s", kb_id, counts)
    return counts
