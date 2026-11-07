"""用户体系、组织标签与文档/块级可见性列。

- app_user：用户（scrypt 口令哈希）
- org_tag / user_org_tag：组织标签与用户打标（多对多）
- kb_document：上传者、可见性（private/public）、组织标签（上传时随文档写入）
- kb_chunk：冗余同三列（与向量同批入库，检索过滤零 JOIN）

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-07
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "app_user",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("password_hash", sa.String(length=200), nullable=False),
        sa.Column("display_name", sa.String(length=100), nullable=False),
        sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_app_user"),
        sa.UniqueConstraint("username", name="uq_app_user_username"),
    )

    op.create_table(
        "org_tag",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_org_tag"),
        sa.UniqueConstraint("name", name="uq_org_tag_name"),
    )

    op.create_table(
        "user_org_tag",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("org_tag_id", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("user_id", "org_tag_id", name="pk_user_org_tag"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["app_user.id"], name="fk_user_org_tag_user_id_app_user"
        ),
        sa.ForeignKeyConstraint(
            ["org_tag_id"], ["org_tag.id"], name="fk_user_org_tag_org_tag_id_org_tag"
        ),
    )

    op.add_column(
        "kb_document",
        sa.Column("uploader_user_id", sa.BigInteger(), nullable=False, server_default=sa.text("1")),
    )
    op.add_column(
        "kb_document",
        sa.Column(
            "visibility", sa.String(length=16), nullable=False, server_default=sa.text("'public'")
        ),
    )
    op.add_column("kb_document", sa.Column("org_tag_id", sa.BigInteger(), nullable=True))
    op.add_column(
        "kb_chunk",
        sa.Column("uploader_user_id", sa.BigInteger(), nullable=False, server_default=sa.text("1")),
    )
    op.add_column(
        "kb_chunk",
        sa.Column(
            "visibility", sa.String(length=16), nullable=False, server_default=sa.text("'public'")
        ),
    )
    op.add_column("kb_chunk", sa.Column("org_tag_id", sa.BigInteger(), nullable=True))
    op.create_check_constraint(
        "ck_kb_document_visibility", "kb_document", "visibility IN ('private','public')"
    )
    op.create_check_constraint(
        "ck_kb_chunk_visibility", "kb_chunk", "visibility IN ('private','public')"
    )
    op.create_index(
        "idx_kb_chunk_visibility", "kb_chunk", ["kb_id", "visibility", "org_tag_id"]
    )


def downgrade() -> None:
    op.drop_index("idx_kb_chunk_visibility", table_name="kb_chunk")
    op.drop_constraint("ck_kb_chunk_visibility", "kb_chunk", type_="check")
    op.drop_constraint("ck_kb_document_visibility", "kb_document", type_="check")
    op.drop_column("kb_chunk", "org_tag_id")
    op.drop_column("kb_chunk", "visibility")
    op.drop_column("kb_chunk", "uploader_user_id")
    op.drop_column("kb_document", "org_tag_id")
    op.drop_column("kb_document", "visibility")
    op.drop_column("kb_document", "uploader_user_id")
    op.drop_table("user_org_tag")
    op.drop_table("org_tag")
    op.drop_table("app_user")
