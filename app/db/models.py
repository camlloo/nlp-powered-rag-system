"""ORM 模型：实现文档 V1.1 附录 A 建表基线 + ingest_job + qa 审计三表。

方言要点：
- PostgreSQL 16/17 + pgvector；向量列使用 pgvector 官方绑定 Vector(1024)，
  禁止应用层拼装二进制（§5.4）。
- 时间列统一 timestamptz 并以 UTC 写入；结构化数据使用 JSONB；
  摘要与哈希使用 BYTEA。
- 跨表关联均带 kb_id（复合外键）；active_index_id 不设外键，
  其同知识库归属与 ready 状态在发布事务中校验，避免循环外键依赖。
- kb_member 为预留表：后期以 JWT RBAC 启用，首期只建表不做业务校验。
- qa_* 为审计表：不设跨表外键，保证审计记录不随业务数据清理级联丢失。
"""

from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import LargeBinary

from app.db.base import Base

# 角色枚举值（预留，后期 RBAC 启用）
ROLES = ("owner", "editor", "viewer")

# ingest_job 状态与阶段（实现文档 §9.4）
JOB_STATUSES = ("queued", "running", "succeeded", "failed", "cancelled")
JOB_STAGES = ("parse", "normalize", "chunk", "embed", "validate", "publish")
JOB_TYPES = ("ingest_file", "reindex", "cleanup")


class AppUser(Base):
    """用户（RBAC）：scrypt 口令哈希，管理员旁路静态令牌。"""

    __tablename__ = "app_user"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(200), nullable=False)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())

    __table_args__ = (UniqueConstraint("username", name="uq_app_user_username"),)


class OrgTag(Base):
    """组织标签（如部门）：用户打标，文档随上传者继承。"""

    __tablename__ = "org_tag"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())

    __table_args__ = (UniqueConstraint("name", name="uq_org_tag_name"),)


class UserOrgTag(Base):
    """用户-组织标签多对多。"""

    __tablename__ = "user_org_tag"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app_user.id"), primary_key=True
    )
    org_tag_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("org_tag.id"), primary_key=True
    )


class KbCollection(Base):
    __tablename__ = "kb_collection"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # 发布事务校验同 kb 归属与 ready 状态，避免循环外键依赖
    active_index_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    data_revision: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    acl_revision: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    deleted_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())

    __table_args__ = (Index("idx_kb_tenant", "tenant_id", "id"),)


class KbMember(Base):
    """预留表：后期以 JWT RBAC 启用成员管理，首期只建表不做业务校验。"""

    __tablename__ = "kb_member"

    kb_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("kb_collection.id"), primary_key=True
    )
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)

    __table_args__ = (
        CheckConstraint("role IN ('owner','editor','viewer')", name="role"),
    )


class KbDocument(Base):
    __tablename__ = "kb_document"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    kb_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("kb_collection.id"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    current_version_no: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    deleted_at: Mapped[datetime | None] = mapped_column(nullable=True)
    # 权限与组织标签：上传时随上传者写入，检索级过滤依据
    uploader_user_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="1"
    )
    visibility: Mapped[str] = mapped_column(String(16), nullable=False, server_default="public")
    org_tag_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    __table_args__ = (
        UniqueConstraint("kb_id", "id", name="uq_kb_document_kb_id"),
        CheckConstraint("visibility IN ('private','public')", name="ck_kb_document_visibility"),
    )


class KbDocumentVersion(Base):
    __tablename__ = "kb_document_version"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    kb_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    document_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    file_sha256: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    storage_uri: Mapped[str] = mapped_column(String(1000), nullable=False)
    parse_status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="pending")
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("document_id", "version_no", name="uq_kb_document_version_document_id"),
        UniqueConstraint("kb_id", "document_id", "id", name="uq_kb_document_version_kb_id"),
        ForeignKeyConstraint(
            ["kb_id", "document_id"],
            ["kb_document.kb_id", "kb_document.id"],
            name="fk_kb_document_version_kb_id_kb_document",
        ),
    )


class KbIndex(Base):
    """一次完整索引快照（非单文件），实现文档 §5.2。"""

    __tablename__ = "kb_index"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    kb_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("kb_collection.id"), nullable=False
    )
    source_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="building")
    embedding_model: Mapped[str] = mapped_column(String(80), nullable=False)
    embedding_dim: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    pipeline_version: Mapped[str] = mapped_column(String(80), nullable=False)
    config_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    manifest_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("kb_id", "id", name="uq_kb_index_kb_id"),
        CheckConstraint("embedding_dim = 1024", name="embedding_dim"),
        Index("idx_index_state", "kb_id", "status", "id"),
    )


class KbChunk(Base):
    __tablename__ = "kb_chunk"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    kb_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    index_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    document_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    doc_version_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chunk_no: Mapped[int] = mapped_column(Integer, nullable=False)
    title_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    search_text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding_input: Mapped[str] = mapped_column(Text, nullable=False)
    input_sha256: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    tokens: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    keywords: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    source_locator: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # 冗余权限列：与向量同批写入，检索级过滤零 JOIN
    uploader_user_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="1"
    )
    visibility: Mapped[str] = mapped_column(String(16), nullable=False, server_default="public")
    org_tag_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "index_id", "doc_version_id", "chunk_no", name="uq_kb_chunk_index_id"
        ),
        CheckConstraint("visibility IN ('private','public')", name="ck_kb_chunk_visibility"),
        Index("idx_kb_chunk_visibility", "kb_id", "visibility", "org_tag_id"),
        ForeignKeyConstraint(
            ["kb_id", "index_id"],
            ["kb_index.kb_id", "kb_index.id"],
            name="fk_kb_chunk_kb_id_kb_index",
        ),
        ForeignKeyConstraint(
            ["kb_id", "document_id", "doc_version_id"],
            [
                "kb_document_version.kb_id",
                "kb_document_version.document_id",
                "kb_document_version.id",
            ],
            name="fk_kb_chunk_kb_id_kb_document_version",
        ),
        Index("idx_chunk_scope", "kb_id", "index_id", "id"),
    )


class KbEmbedding(Base):
    __tablename__ = "kb_embedding"

    chunk_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("kb_chunk.id"), primary_key=True
    )
    embedding: Mapped[Any] = mapped_column(Vector(1024), nullable=False)


class IngestJob(Base):
    """后台任务与租约（实现文档 §9.4）：崩溃后按租约过期恢复。"""

    __tablename__ = "ingest_job"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    kb_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("kb_collection.id"), nullable=False
    )
    doc_version_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("kb_document_version.id"), nullable=True
    )
    job_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="queued")
    stage: Mapped[str | None] = mapped_column(String(32), nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    max_attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default="3")
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())

    __table_args__ = (
        Index("idx_job_claim", "status", "lease_until"),
        Index("idx_job_kb_state", "kb_id", "status", "id"),
    )


class Conversation(Base):
    """任务/会话：多轮问答的分组，供前端任务列表展示与历史还原。"""

    __tablename__ = "conversation"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())

    __table_args__ = (Index("idx_conversation_user_time", "user_id", "updated_at"),)


class QaRequest(Base):
    """问答审计：不设跨表外键，保证审计记录独立于业务数据清理。"""

    __tablename__ = "qa_request"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kb_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    index_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # 会话分组（审计表无外键：会话删除后保留为悬空历史值）
    conversation_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer_status: Mapped[str] = mapped_column(String(32), nullable=False)
    retrieval_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    model_config_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    usage_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("request_id", name="uq_qa_request_request_id"),
        Index("idx_qa_request_kb_time", "kb_id", "created_at"),
        Index("idx_qa_request_conversation", "conversation_id", "id"),
    )


class QaCitation(Base):
    __tablename__ = "qa_citation"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    qa_request_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("qa_request.id"), nullable=False
    )
    source_id: Mapped[str] = mapped_column(String(8), nullable=False)
    citation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    chunk_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    doc_version_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    document_title: Mapped[str] = mapped_column(String(300), nullable=False)
    locator: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    quote: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint("citation_id", name="uq_qa_citation_citation_id"),
    )


class QaFeedback(Base):
    __tablename__ = "qa_feedback"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    qa_request_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("qa_request.id"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    rating: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint("rating IN ('helpful','unhelpful')", name="rating"),
    )
