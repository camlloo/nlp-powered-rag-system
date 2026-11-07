"""索引快照构建与原子发布（实现文档 §5.2、§9.4）。

一次构建 = 一个知识库的完整可检索快照（全量重建，首期策略）：
  parse（新文件）-> manifest 固定清单 -> chunk（逐文件 NLP 流水线）
  -> embed（批量向量，缓存复用未变化文件）-> validate -> publish（短事务）

一致性红线：
- 解析状态变化与 data_revision 同事务更新；解析后记录修订号并固定文件清单，
  发布前锁定知识库行复核，修订号变化或快照被替代则放弃发布；
- 失败快照不替换现有索引，旧索引持续服务；needs_review 不进入清单；
- 远程模型调用不持有数据库锁；提交阶段校验租约所有者；
- 处理按文件版本及流水线指纹幂等（向量网关按 embedding_input 哈希复用）。
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from sqlalchemy import func, select, update
from sqlalchemy.orm import sessionmaker

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
from app.gateways.embedding import EmbeddingGateway, TokenUsage
from app.gateways.mock import MockEmbeddingGateway
from app.nlp.pipeline import pipeline_version, run_pipeline
from app.parsers.intake import load_upload, parse_upload
from app.schemas.contracts import ParsedDocument
from app.storage import parse_object_uri

logger = logging.getLogger(__name__)

PARSE_SUCCEEDED = "succeeded"
PARSE_NEEDS_REVIEW = "needs_review"
PARSE_FAILED = "failed"

EMBED_BATCH_ROWS = 200  # 每批入库的 chunk 行数

STAGE_PARSE = "parse"
STAGE_CHUNK = "chunk"
STAGE_EMBED = "embed"
STAGE_VALIDATE = "validate"
STAGE_PUBLISH = "publish"


def _parse_filename(title: str, storage_uri: str, settings: Settings) -> str:
    """使用解码后的对象原文件名确定扩展名，保留数据库标题作为解析标题。"""
    key = parse_object_uri(storage_uri, bucket=settings.minio_bucket)
    return f"{title}{PurePosixPath(key).suffix}"


class RevisionChanged(Exception):
    """快照已失效或被替代，不能发布。"""


class LeaseLost(Exception):
    """租约已被其他 worker 接管：立即放弃当前执行。"""


@dataclass
class LeaseKeeper:
    """任务租约维持：阶段间与长批次间续租，提交前校验所有者（§9.4）。"""

    session_factory: sessionmaker
    worker_id: str
    lease_seconds: int
    job_id: int | None = None

    def bind(self, job_id: int) -> None:
        self.job_id = job_id

    def renew(self) -> None:
        if self.job_id is None:
            return
        with self.session_factory() as session:
            result = session.execute(
                update(IngestJob)
                .where(
                    IngestJob.id == self.job_id,
                    IngestJob.lease_owner == self.worker_id,
                    IngestJob.status == "running",
                )
                .values(
                    heartbeat_at=func.now(),
                    lease_until=func.now()
                    + func.make_interval(0, 0, 0, 0, 0, 0, self.lease_seconds),
                )
            )
            session.commit()
            if result.rowcount == 0:
                raise LeaseLost(f"job={self.job_id} 租约已丢失")

    def set_stage(self, stage: str) -> None:
        if self.job_id is None:
            return
        with self.session_factory() as session:
            session.execute(
                update(IngestJob)
                .where(
                    IngestJob.id == self.job_id,
                    IngestJob.lease_owner == self.worker_id,
                    IngestJob.status == "running",
                )
                .values(stage=stage, heartbeat_at=func.now())
            )
            session.commit()


@dataclass
class _FileUnit:
    doc_version_id: int
    document_id: int
    title: str
    storage_uri: str
    filename: str
    file_sha256: bytes
    uploader_user_id: int = 1
    visibility: str = "public"
    org_tag_id: int | None = None
    parsed: ParsedDocument | None = None


@dataclass
class _DraftRow:
    doc_version_id: int
    document_id: int
    draft: object  # ChunkDraft
    uploader_user_id: int = 1
    visibility: str = "public"
    org_tag_id: int | None = None


@dataclass
class BuildReport:
    index_id: int
    files: int
    chunks: int
    usage: TokenUsage = field(default_factory=TokenUsage)
    cache_hits: int = 0


class IndexBuilder:
    def __init__(
        self,
        session_factory: sessionmaker,
        embedding: EmbeddingGateway | MockEmbeddingGateway,
        settings: Settings,
        *,
        worker_id: str = "builder",
    ) -> None:
        self._sessions = session_factory
        self._embedding = embedding
        self._settings = settings
        self._worker_id = worker_id

    async def build(
        self,
        *,
        kb_id: int,
        new_doc_version_id: int | None = None,
        lease: LeaseKeeper | None = None,
    ) -> BuildReport:
        with self._sessions() as session:
            kb = session.get(KbCollection, kb_id)
            if kb is None:
                raise AppError("知识库不存在", code="not_found", status_code=404)
            tenant_id = kb.tenant_id

        # stage: parse —— 仅新上传文件；失败则任务失败，旧索引持续服务
        if lease:
            lease.set_stage(STAGE_PARSE)
        if new_doc_version_id is not None:
            self._parse_and_mark(kb_id, new_doc_version_id)

        # 固定清单（仅 succeeded、未删除）与修订号
        with self._sessions() as session:
            kb = session.get(KbCollection, kb_id)
            source_revision = kb.data_revision
            manifest_rows = session.execute(
                select(KbDocumentVersion, KbDocument)
                .join(KbDocument, KbDocument.id == KbDocumentVersion.document_id)
                .where(
                    KbDocumentVersion.kb_id == kb_id,
                    KbDocument.deleted_at.is_(None),
                    KbDocumentVersion.parse_status == PARSE_SUCCEEDED,
                )
                .order_by(KbDocumentVersion.id)
            ).all()
        units = [
            _FileUnit(
                doc_version_id=version.id,
                document_id=version.document_id,
                title=version.title,
                storage_uri=version.storage_uri,
                filename=_parse_filename(version.title, version.storage_uri, self._settings),
                file_sha256=bytes(version.file_sha256),
                uploader_user_id=document.uploader_user_id,
                visibility=document.visibility,
                org_tag_id=document.org_tag_id,
            )
            for version, document in manifest_rows
        ]

        index_id = self._create_index_row(kb_id, source_revision, units, tenant_id)

        # stage: chunk —— 逐文件跑传统 NLP 流水线（确定性，可重复）
        if lease:
            lease.set_stage(STAGE_CHUNK)
        draft_rows: list[_DraftRow] = []
        per_file_counts: dict[int, int] = {}
        for unit in units:
            payload = self._load_verified(
                kb_id, unit.document_id, unit.doc_version_id, unit.storage_uri, unit.file_sha256
            )
            parsed = parse_upload(unit.filename, None, payload, self._settings)
            result = run_pipeline(parsed, self._settings)
            for draft in result.chunks:
                draft_rows.append(
                    _DraftRow(
                        unit.doc_version_id,
                        unit.document_id,
                        draft,
                        uploader_user_id=unit.uploader_user_id,
                        visibility=unit.visibility,
                        org_tag_id=unit.org_tag_id,
                    )
                )
            per_file_counts[unit.doc_version_id] = len(result.chunks)
            if lease:
                lease.renew()

        # stage: embed —— 批量向量；未变化文件经输入哈希命中缓存复用（§5.2）
        if lease:
            lease.set_stage(STAGE_EMBED)
        usage = TokenUsage()
        cache_hits = 0
        if draft_rows:
            inputs = [row.draft.embedding_input for row in draft_rows]
            result = await self._embedding.embed_texts(
                inputs,
                tenant_id=tenant_id,
                budget_scope_id=f"index-{index_id}",
            )
            usage, cache_hits = result.usage, result.cache_hits
            self._insert_chunks(index_id, draft_rows, result.vectors, lease)

        # stage: validate —— 块数、向量数、向量维度（实现文档 §5.2）
        if lease:
            lease.set_stage(STAGE_VALIDATE)
        self._validate(index_id, len(draft_rows))
        self._store_manifest(index_id, per_file_counts, usage, cache_hits)

        # stage: publish —— 短事务，锁定知识库行核对修订号
        if lease:
            lease.set_stage(STAGE_PUBLISH)
            lease.renew()
        self._publish(kb_id, index_id, source_revision)
        logger.info(
            "snapshot published: kb=%s index=%s files=%s chunks=%s cache_hits=%s",
            kb_id,
            index_id,
            len(units),
            len(draft_rows),
            cache_hits,
        )
        return BuildReport(
            index_id=index_id,
            files=len(units),
            chunks=len(draft_rows),
            usage=usage,
            cache_hits=cache_hits,
        )

    # ------------------------------------------------------------------ stages

    def _assert_live_version(
        self, kb_id: int, document_id: int, doc_version_id: int, storage_uri: str, digest: bytes
    ) -> None:
        """读前和读后检查删除状态与清单是否仍对应同一版本。"""
        with self._sessions() as session:
            kb = session.get(KbCollection, kb_id)
            document = session.get(KbDocument, document_id)
            version = session.get(KbDocumentVersion, doc_version_id)
            if (
                kb is None
                or kb.deleted_at is not None
                or document is None
                or document.kb_id != kb_id
                or document.deleted_at is not None
                or version is None
                or version.kb_id != kb_id
                or version.document_id != document_id
                or version.storage_uri != storage_uri
                or version.file_sha256 != digest
            ):
                raise AppError("文件已删除或版本已变化", code="document_changed", status_code=409)

    def _load_verified(
        self, kb_id: int, document_id: int, doc_version_id: int, storage_uri: str, digest: bytes
    ) -> bytes:
        self._assert_live_version(kb_id, document_id, doc_version_id, storage_uri, digest)
        payload = load_upload(storage_uri, self._settings)
        if len(digest) != 32 or hashlib.sha256(payload).digest() != digest:
            raise AppError("文件内容与版本摘要不一致", code="file_digest_mismatch", status_code=409)
        self._assert_live_version(kb_id, document_id, doc_version_id, storage_uri, digest)
        return payload

    def _parse_and_mark(self, kb_id: int, doc_version_id: int) -> None:
        """解析新文件并落 parse_status；needs_review 不进入清单，失败则任务失败。"""
        with self._sessions() as session:
            row = session.get(KbDocumentVersion, doc_version_id)
            if row is None or row.kb_id != kb_id:
                raise AppError("文件版本不存在", code="not_found", status_code=404)
            title = row.title
            document_id = row.document_id
            storage_uri = row.storage_uri
            digest = bytes(row.file_sha256)
        try:
            filename = _parse_filename(title, storage_uri, self._settings)
            payload = self._load_verified(kb_id, document_id, doc_version_id, storage_uri, digest)
            parsed = parse_upload(filename, None, payload, self._settings)
        except AppError as exc:
            self._mark_parse_status(doc_version_id, PARSE_FAILED)
            raise AppError(
                f"文件解析失败：{exc.message}", code="parse_failed", status_code=422
            ) from exc
        except Exception as exc:  # noqa: BLE001 解析器未捕获异常统一转为失败状态
            self._mark_parse_status(doc_version_id, PARSE_FAILED)
            raise AppError(
                f"文件解析失败：{exc.__class__.__name__}", code="parse_failed", status_code=422
            ) from exc
        quality = parsed.quality
        status = PARSE_NEEDS_REVIEW if (quality and quality.needs_review) else PARSE_SUCCEEDED
        self._mark_parse_status(doc_version_id, status)

    def _mark_parse_status(self, doc_version_id: int, status: str) -> None:
        with self._sessions() as session:
            # 条件更新在行锁下判断是否真的变化，避免并发重复解析重复递增。
            kb_id = session.execute(
                update(KbDocumentVersion)
                .where(
                    KbDocumentVersion.id == doc_version_id,
                    KbDocumentVersion.parse_status != status,
                )
                .values(parse_status=status)
                .returning(KbDocumentVersion.kb_id)
            ).scalar_one_or_none()
            if kb_id is not None:
                session.execute(
                    update(KbCollection)
                    .where(KbCollection.id == kb_id)
                    .values(data_revision=KbCollection.data_revision + 1)
                )
            session.commit()

    def _create_index_row(
        self, kb_id: int, source_revision: int, units: list[_FileUnit], tenant_id: int
    ) -> int:
        with self._sessions() as session:
            # 先锁住知识库并校验修订号，防止旧清单在新构建开始后替代新快照。
            kb = session.execute(
                select(KbCollection).where(KbCollection.id == kb_id).with_for_update()
            ).scalar_one()
            if kb.deleted_at is not None or kb.data_revision != source_revision:
                raise RevisionChanged(f"kb={kb_id} 构建清单已失效")
            # 放弃同知识库遗留的 building 快照；building 不参与检索。
            session.execute(
                update(KbIndex)
                .where(KbIndex.kb_id == kb_id, KbIndex.status == "building")
                .values(status="superseded")
            )
            index = KbIndex(
                kb_id=kb_id,
                source_revision=source_revision,
                status="building",
                embedding_model=self._settings.embedding_model,
                embedding_dim=self._settings.embedding_dim,
                pipeline_version=pipeline_version(self._settings),
                config_json={
                    "chunk_max_chars": self._settings.chunk_max_chars,
                    "chunk_overlap_chars": self._settings.chunk_overlap_chars,
                    "vector_backend": self._settings.vector_backend,
                    "tenant_id": tenant_id,
                },
                manifest_json={"files": [u.doc_version_id for u in units]},
            )
            session.add(index)
            session.commit()
            return index.id

    def _insert_chunks(
        self,
        index_id: int,
        draft_rows: list[_DraftRow],
        vectors,
        lease: LeaseKeeper | None,
    ) -> None:
        counters: dict[int, int] = {}
        with self._sessions() as session:
            kb_id = session.get(KbIndex, index_id).kb_id
            batch: list[KbChunk] = []

            def flush() -> None:
                nonlocal batch
                if not batch:
                    return
                offset = session.query(func.count(KbChunk.id)).filter(
                    KbChunk.index_id == index_id
                ).scalar() or 0
                session.add_all(batch)
                session.flush()
                for i, chunk in enumerate(batch):
                    session.add(KbEmbedding(chunk_id=chunk.id, embedding=vectors[offset + i]))
                session.commit()
                batch = []

            for row in draft_rows:
                draft = row.draft
                chunk_no = counters.get(row.doc_version_id, 0)
                counters[row.doc_version_id] = chunk_no + 1
                batch.append(
                    KbChunk(
                        kb_id=kb_id,
                        index_id=index_id,
                        document_id=row.document_id,
                        doc_version_id=row.doc_version_id,
                        chunk_no=chunk_no,
                        title_path=" > ".join(draft.title_path),
                        content=draft.content,
                        search_text=draft.search_text,
                        embedding_input=draft.embedding_input,
                        input_sha256=hashlib.sha256(
                            draft.embedding_input.encode("utf-8")
                        ).digest(),
                        tokens=list(draft.tokens),
                        keywords=list(draft.keywords),
                        source_locator=dict(draft.source_locator),
                        uploader_user_id=row.uploader_user_id,
                        visibility=row.visibility,
                        org_tag_id=row.org_tag_id,
                    )
                )
                if len(batch) >= EMBED_BATCH_ROWS:
                    flush()
                    if lease:
                        lease.renew()
            flush()

    def _validate(self, index_id: int, expected_chunks: int) -> None:
        with self._sessions() as session:
            chunk_count = session.execute(
                select(func.count()).select_from(KbChunk).where(KbChunk.index_id == index_id)
            ).scalar_one()
            embedding_count = session.execute(
                select(func.count())
                .select_from(KbEmbedding)
                .join(KbChunk, KbChunk.id == KbEmbedding.chunk_id)
                .where(KbChunk.index_id == index_id)
            ).scalar_one()
            bad_dims = session.execute(
                select(func.count())
                .select_from(KbEmbedding)
                .join(KbChunk, KbChunk.id == KbEmbedding.chunk_id)
                .where(
                    KbChunk.index_id == index_id,
                    func.vector_dims(KbEmbedding.embedding) != self._settings.embedding_dim,
                )
            ).scalar_one()
        if chunk_count != expected_chunks:
            raise AppError(
                f"块数不一致：库内 {chunk_count} != 预期 {expected_chunks}",
                code="snapshot_invalid",
                status_code=500,
            )
        if embedding_count != expected_chunks:
            raise AppError(
                f"向量数不一致：{embedding_count} != 块数 {expected_chunks}",
                code="snapshot_invalid",
                status_code=500,
            )
        if bad_dims:
            raise AppError(
                f"存在 {bad_dims} 条维度不符的向量",
                code="snapshot_invalid",
                status_code=500,
            )
        logger.info("snapshot validated: index=%s chunks=%s", index_id, chunk_count)

    def _store_manifest(
        self, index_id: int, per_file_counts: dict[int, int], usage: TokenUsage, cache_hits: int
    ) -> None:
        with self._sessions() as session:
            index = session.get(KbIndex, index_id)
            manifest = dict(index.manifest_json)
            manifest["chunk_counts"] = {str(k): v for k, v in per_file_counts.items()}
            manifest["totals"] = {
                "chunks": sum(per_file_counts.values()),
                "embedding_prompt_tokens": usage.prompt_tokens,
                "embedding_total_tokens": usage.total_tokens,
                "vector_cache_hits": cache_hits,
            }
            index.manifest_json = manifest
            session.commit()

    def _publish(self, kb_id: int, index_id: int, source_revision: int) -> None:
        with self._sessions() as session:
            kb = session.execute(
                select(KbCollection).where(KbCollection.id == kb_id).with_for_update()
            ).scalar_one()
            if kb.deleted_at is not None or kb.data_revision != source_revision:
                session.execute(
                    update(KbIndex)
                    .where(
                        KbIndex.id == index_id,
                        KbIndex.kb_id == kb_id,
                        KbIndex.status == "building",
                    )
                    .values(status="superseded")
                )
                session.commit()
                raise RevisionChanged(
                    f"kb={kb_id} 已删除或修订号变化："
                    f"{source_revision} -> {kb.data_revision}，快照不可发布"
                )
            result = session.execute(
                update(KbIndex)
                .where(
                    KbIndex.id == index_id,
                    KbIndex.kb_id == kb_id,
                    KbIndex.source_revision == source_revision,
                    KbIndex.status == "building",
                )
                .values(status="ready")
            )
            if result.rowcount != 1:
                raise RevisionChanged(f"kb={kb_id} index={index_id} 已被替代或不再可发布")
            kb.active_index_id = index_id
            session.commit()
