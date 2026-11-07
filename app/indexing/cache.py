"""运行时索引缓存装载（实现文档 §3.2、附录 B1）。

MySQL/PostgreSQL 是唯一持久化来源，本缓存是可重建的运行时副本：
- 每个知识库保留最近装载的快照，swap 原子替换；
- 装载按附录 B1 语义过滤（ready 快照、未删除文档、租户限定）；
- 装载前后复核 active_index_id 与修订号，变化即 Conflict 由调用方重试；
- 超出内存预算直接报错，不允许静默截断（§7.3）。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from functools import lru_cache

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings, get_settings
from app.core.errors import Conflict
from app.db.models import KbChunk, KbCollection, KbDocument, KbEmbedding, KbIndex
from app.db.session import SessionLocal

logger = logging.getLogger(__name__)


@dataclass
class ChunkRecord:
    chunk_id: int
    doc_version_id: int
    title_path: str
    content: str
    search_text: str
    tokens: list[str]
    keywords: list[str]
    source_locator: dict


@dataclass
class LoadedIndex:
    kb_id: int
    index_id: int
    tenant_id: int
    data_revision: int
    acl_revision: int
    chunk_ids: list[int] = field(default_factory=list)
    matrix: np.ndarray = field(default_factory=lambda: np.empty((0, 0), dtype=np.float32))
    records: dict[int, ChunkRecord] = field(default_factory=dict)
    # 派生运行时结构（如 BM25 索引）由检索侧按需构建并挂载，随快照换入换出
    derived: dict[str, object] = field(default_factory=dict)

    def estimate_bytes(self) -> int:
        text_bytes = sum(
            len(r.content) + len(r.search_text) + len(r.title_path) + 32 * len(r.tokens)
            for r in self.records.values()
        )
        return int(self.matrix.nbytes + text_bytes * 2 + 200 * len(self.records))


class IndexCacheManager:
    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._loaded: dict[int, LoadedIndex] = {}

    def get(self, kb_id: int) -> LoadedIndex | None:
        return self._loaded.get(kb_id)

    def swap(self, index: LoadedIndex) -> None:
        self._loaded[index.kb_id] = index

    async def ensure_loaded(
        self,
        *,
        session_factory: sessionmaker = SessionLocal,
        kb_id: int,
        tenant_id: int,
        expected_index_id: int,
        expected_data_revision: int,
        expected_acl_revision: int,
    ) -> LoadedIndex:
        """调用方先读知识库行得到 expected_*；缓存匹配直接返回，否则装载并复核。"""
        current = self._loaded.get(kb_id)
        if current and (
            current.index_id,
            current.data_revision,
            current.acl_revision,
        ) == (
            expected_index_id,
            expected_data_revision,
            expected_acl_revision,
        ):
            return current
        index = await asyncio.to_thread(
            self._load, session_factory, kb_id, tenant_id, expected_index_id
        )
        # 装载后复核：装载期间发布/删除/撤权变化则放弃本次装载
        with session_factory() as session:
            kb = session.get(KbCollection, kb_id)
            if (
                kb is None
                or kb.tenant_id != tenant_id
                or kb.active_index_id != expected_index_id
                or kb.data_revision != expected_data_revision
                or kb.acl_revision != expected_acl_revision
            ):
                raise Conflict("索引装载期间知识库状态发生变化，请重试")
        self.swap(index)
        logger.info(
            "index cache loaded: kb=%s index=%s chunks=%s est=%.1fMiB",
            kb_id,
            expected_index_id,
            len(index.chunk_ids),
            index.estimate_bytes() / 1048576,
        )
        return index

    def _check_budget(self, estimate_bytes: int) -> None:
        budget = self._settings.index_cache_budget_mb * 1048576
        if budget and estimate_bytes > budget:
            raise Conflict(
                f"索引缓存估算 {estimate_bytes} 字节超出预算 {budget} 字节；"
                "不允许静默截断，请缩小知识库或提高预算"
            )

    def _load(
        self, session_factory: sessionmaker, kb_id: int, tenant_id: int, index_id: int
    ) -> LoadedIndex:
        with session_factory() as session:
            kb = session.get(KbCollection, kb_id)
            if kb is None or kb.tenant_id != tenant_id:
                raise Conflict("知识库不存在或不属于当前租户")
            rows = session.execute(
                select(KbChunk, KbEmbedding.embedding)
                .join(KbEmbedding, KbEmbedding.chunk_id == KbChunk.id)
                .join(KbIndex, (KbIndex.id == KbChunk.index_id) & (KbIndex.kb_id == KbChunk.kb_id))
                .join(
                    KbDocument,
                    (KbDocument.id == KbChunk.document_id) & (KbDocument.kb_id == KbChunk.kb_id),
                )
                .join(KbCollection, KbCollection.id == KbChunk.kb_id)
                .where(
                    KbChunk.kb_id == kb_id,
                    KbChunk.index_id == index_id,
                    KbIndex.status == "ready",
                    KbDocument.deleted_at.is_(None),
                    KbCollection.tenant_id == tenant_id,
                )
                .order_by(KbChunk.id)
            ).all()

        index = LoadedIndex(
            kb_id=kb_id,
            index_id=index_id,
            tenant_id=tenant_id,
            data_revision=kb.data_revision,
            acl_revision=kb.acl_revision,
        )
        embeddings: list[np.ndarray] = []
        for chunk, embedding in rows:
            index.chunk_ids.append(chunk.id)
            index.records[chunk.id] = ChunkRecord(
                chunk_id=chunk.id,
                doc_version_id=chunk.doc_version_id,
                title_path=chunk.title_path,
                content=chunk.content,
                search_text=chunk.search_text,
                tokens=list(chunk.tokens),
                keywords=list(chunk.keywords),
                source_locator=dict(chunk.source_locator),
            )
            embeddings.append(np.asarray(embedding, dtype=np.float32))
        if embeddings:
            index.matrix = np.stack(embeddings).astype(np.float32)
        else:
            index.matrix = np.empty((0, self._settings.embedding_dim), dtype=np.float32)
        self._check_budget(index.estimate_bytes())
        return index


@lru_cache
def get_index_cache() -> IndexCacheManager:
    return IndexCacheManager()
