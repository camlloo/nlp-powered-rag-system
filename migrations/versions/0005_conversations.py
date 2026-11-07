"""任务/会话：多轮问答归入 conversation，供侧边栏任务列表与历史还原。

qa_request 为审计表（不设跨表外键），conversation_id 仅作分组关联；
引用还原所需的 document_version 在读取时 join kb_document_version 得到。
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "conversation",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("tenant_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("title", sa.String(length=120), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name="pk_conversation"),
    )
    op.create_index("idx_conversation_user_time", "conversation", ["user_id", "updated_at"])
    op.add_column("qa_request", sa.Column("conversation_id", sa.BigInteger(), nullable=True))
    op.create_index("idx_qa_request_conversation", "qa_request", ["conversation_id", "id"])


def downgrade() -> None:
    op.drop_index("idx_qa_request_conversation", table_name="qa_request")
    op.drop_column("qa_request", "conversation_id")
    op.drop_index("idx_conversation_user_time", table_name="conversation")
    op.drop_table("conversation")
