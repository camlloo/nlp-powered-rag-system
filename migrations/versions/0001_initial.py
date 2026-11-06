"""初始建表基线：核心表 + ingest_job + qa 审计三表，并安装 pgvector 扩展。

对应实现文档 V1.1 附录 A；ingest_job 与 qa_* 为任务租约与审计补充表。

Revision ID: 0001
Revises:
Create Date: 2026-11-07
"""

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 开发环境单账号，扩展随迁移安装；生产分权时由迁移角色执行
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "kb_collection",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("tenant_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("active_index_id", sa.BigInteger(), nullable=True),
        sa.Column("data_revision", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("acl_revision", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_kb_collection"),
    )
    op.create_index("idx_kb_tenant", "kb_collection", ["tenant_id", "id"], unique=False)

    # 预留表：后期以 JWT RBAC 启用成员管理，首期只建表不做业务校验
    op.create_table(
        "kb_member",
        sa.Column("kb_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.PrimaryKeyConstraint("kb_id", "user_id", name="pk_kb_member"),
        sa.ForeignKeyConstraint(
            ["kb_id"], ["kb_collection.id"], name="fk_kb_member_kb_id_kb_collection"
        ),
        sa.CheckConstraint("role IN ('owner','editor','viewer')", name="ck_kb_member_role"),
    )

    op.create_table(
        "kb_document",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("kb_id", sa.BigInteger(), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("current_version_no", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_kb_document"),
        sa.ForeignKeyConstraint(
            ["kb_id"], ["kb_collection.id"], name="fk_kb_document_kb_id_kb_collection"
        ),
        sa.UniqueConstraint("kb_id", "id", name="uq_kb_document_kb_id"),
    )

    op.create_table(
        "kb_document_version",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("kb_id", sa.BigInteger(), nullable=False),
        sa.Column("document_id", sa.BigInteger(), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("file_sha256", sa.LargeBinary(), nullable=False),
        sa.Column("storage_uri", sa.String(length=1000), nullable=False),
        sa.Column("parse_status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_kb_document_version"),
        sa.ForeignKeyConstraint(
            ["kb_id", "document_id"],
            ["kb_document.kb_id", "kb_document.id"],
            name="fk_kb_document_version_kb_id_kb_document",
        ),
        sa.UniqueConstraint(
            "document_id", "version_no", name="uq_kb_document_version_document_id"
        ),
        sa.UniqueConstraint("kb_id", "document_id", "id", name="uq_kb_document_version_kb_id"),
    )

    op.create_table(
        "kb_index",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("kb_id", sa.BigInteger(), nullable=False),
        sa.Column("source_revision", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="building"),
        sa.Column("embedding_model", sa.String(length=80), nullable=False),
        sa.Column("embedding_dim", sa.SmallInteger(), nullable=False),
        sa.Column("pipeline_version", sa.String(length=80), nullable=False),
        sa.Column("config_json", JSONB(), nullable=False),
        sa.Column("manifest_json", JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_kb_index"),
        sa.ForeignKeyConstraint(
            ["kb_id"], ["kb_collection.id"], name="fk_kb_index_kb_id_kb_collection"
        ),
        sa.UniqueConstraint("kb_id", "id", name="uq_kb_index_kb_id"),
        sa.CheckConstraint("embedding_dim = 1024", name="ck_kb_index_embedding_dim"),
    )
    op.create_index("idx_index_state", "kb_index", ["kb_id", "status", "id"], unique=False)

    op.create_table(
        "kb_chunk",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("kb_id", sa.BigInteger(), nullable=False),
        sa.Column("index_id", sa.BigInteger(), nullable=False),
        sa.Column("document_id", sa.BigInteger(), nullable=False),
        sa.Column("doc_version_id", sa.BigInteger(), nullable=False),
        sa.Column("chunk_no", sa.Integer(), nullable=False),
        sa.Column("title_path", sa.String(length=1000), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("search_text", sa.Text(), nullable=False),
        sa.Column("embedding_input", sa.Text(), nullable=False),
        sa.Column("input_sha256", sa.LargeBinary(), nullable=False),
        sa.Column("tokens", JSONB(), nullable=False),
        sa.Column("keywords", JSONB(), nullable=False),
        sa.Column("source_locator", JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_kb_chunk"),
        sa.ForeignKeyConstraint(
            ["kb_id", "index_id"],
            ["kb_index.kb_id", "kb_index.id"],
            name="fk_kb_chunk_kb_id_kb_index",
        ),
        sa.ForeignKeyConstraint(
            ["kb_id", "document_id", "doc_version_id"],
            [
                "kb_document_version.kb_id",
                "kb_document_version.document_id",
                "kb_document_version.id",
            ],
            name="fk_kb_chunk_kb_id_kb_document_version",
        ),
        sa.UniqueConstraint(
            "index_id", "doc_version_id", "chunk_no", name="uq_kb_chunk_index_id"
        ),
    )
    op.create_index("idx_chunk_scope", "kb_chunk", ["kb_id", "index_id", "id"], unique=False)

    op.create_table(
        "kb_embedding",
        sa.Column("chunk_id", sa.BigInteger(), nullable=False),
        sa.Column("embedding", Vector(1024), nullable=False),
        sa.PrimaryKeyConstraint("chunk_id", name="pk_kb_embedding"),
        sa.ForeignKeyConstraint(
            ["chunk_id"], ["kb_chunk.id"], name="fk_kb_embedding_chunk_id_kb_chunk"
        ),
    )

    op.create_table(
        "ingest_job",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("kb_id", sa.BigInteger(), nullable=False),
        sa.Column("doc_version_id", sa.BigInteger(), nullable=True),
        sa.Column("job_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="queued"),
        sa.Column("stage", sa.String(length=32), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempt", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("payload_json", JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ingest_job"),
        sa.ForeignKeyConstraint(
            ["kb_id"], ["kb_collection.id"], name="fk_ingest_job_kb_id_kb_collection"
        ),
        sa.ForeignKeyConstraint(
            ["doc_version_id"],
            ["kb_document_version.id"],
            name="fk_ingest_job_doc_version_id_kb_document_version",
        ),
    )
    op.create_index("idx_job_claim", "ingest_job", ["status", "lease_until"], unique=False)
    op.create_index(
        "idx_job_kb_state", "ingest_job", ["kb_id", "status", "id"], unique=False
    )

    # 审计表：不设跨表外键，保证审计记录独立于业务数据清理
    op.create_table(
        "qa_request",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("kb_id", sa.BigInteger(), nullable=False),
        sa.Column("index_id", sa.BigInteger(), nullable=True),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("answer_status", sa.String(length=32), nullable=False),
        sa.Column("retrieval_mode", sa.String(length=32), nullable=True),
        sa.Column("answer", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("model_config_json", JSONB(), nullable=False),
        sa.Column("usage_json", JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_qa_request"),
        sa.UniqueConstraint("request_id", name="uq_qa_request_request_id"),
    )
    op.create_index(
        "idx_qa_request_kb_time", "qa_request", ["kb_id", "created_at"], unique=False
    )

    op.create_table(
        "qa_citation",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("qa_request_id", sa.BigInteger(), nullable=False),
        sa.Column("source_id", sa.String(length=8), nullable=False),
        sa.Column("citation_id", sa.String(length=64), nullable=False),
        sa.Column("chunk_id", sa.BigInteger(), nullable=False),
        sa.Column("doc_version_id", sa.BigInteger(), nullable=False),
        sa.Column("document_title", sa.String(length=300), nullable=False),
        sa.Column("locator", JSONB(), nullable=False),
        sa.Column("quote", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_qa_citation"),
        sa.ForeignKeyConstraint(
            ["qa_request_id"], ["qa_request.id"], name="fk_qa_citation_qa_request_id_qa_request"
        ),
        sa.UniqueConstraint("citation_id", name="uq_qa_citation_citation_id"),
    )

    op.create_table(
        "qa_feedback",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("qa_request_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("rating", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_qa_feedback"),
        sa.ForeignKeyConstraint(
            ["qa_request_id"], ["qa_request.id"], name="fk_qa_feedback_qa_request_id_qa_request"
        ),
        sa.CheckConstraint("rating IN ('helpful','unhelpful')", name="ck_qa_feedback_rating"),
    )


def downgrade() -> None:
    op.drop_table("qa_feedback")
    op.drop_table("qa_citation")
    op.drop_index("idx_qa_request_kb_time", table_name="qa_request")
    op.drop_table("qa_request")
    op.drop_index("idx_job_kb_state", table_name="ingest_job")
    op.drop_index("idx_job_claim", table_name="ingest_job")
    op.drop_table("ingest_job")
    op.drop_table("kb_embedding")
    op.drop_index("idx_chunk_scope", table_name="kb_chunk")
    op.drop_table("kb_chunk")
    op.drop_index("idx_index_state", table_name="kb_index")
    op.drop_table("kb_index")
    op.drop_table("kb_document_version")
    op.drop_table("kb_document")
    op.drop_table("kb_member")
    op.drop_index("idx_kb_tenant", table_name="kb_collection")
    op.drop_table("kb_collection")
    # 不卸载 pgvector 扩展，避免影响同库其他对象
