"""固定对象 key 的持久写入屏障与上传幂等结果。"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "upload_object_guard",
        sa.Column("uri_hash", sa.String(64), primary_key=True),
        sa.Column("uri", sa.String(1000), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("token", sa.String(32), nullable=True),
        sa.CheckConstraint("state IN ('ready','writing')", name="ck_upload_object_guard_state"),
        sa.CheckConstraint(
            "(state = 'ready' AND token IS NULL) OR (state = 'writing' AND token IS NOT NULL)",
            name="ck_upload_object_guard_token",
        ),
    )
    op.create_table(
        "upload_idempotency",
        sa.Column("identity_hash", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("route", sa.String(200), nullable=False),
        sa.Column("request_key", sa.String(512), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("token", sa.String(32), nullable=True),
        sa.Column("job_id", sa.BigInteger(), nullable=True),
        sa.Column("document_id", sa.BigInteger(), nullable=True),
        sa.Column("version_no", sa.BigInteger(), nullable=True),
        sa.Column("kb_id", sa.BigInteger(), nullable=True),
        sa.CheckConstraint("state IN ('pending','completed')", name="ck_upload_idempotency_state"),
        sa.CheckConstraint(
            "(state = 'pending' AND token IS NOT NULL) OR "
            "(state = 'completed' AND token IS NULL AND job_id IS NOT NULL "
            "AND document_id IS NOT NULL AND version_no IS NOT NULL AND kb_id IS NOT NULL)",
            name="ck_upload_idempotency_result",
        ),
    )


def downgrade() -> None:
    # 删除 writing 屏障会让结果不确定的对象再次被写入，故必须先人工处理所有阻塞。
    connection = op.get_bind()
    blocked = connection.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM upload_object_guard WHERE state = 'writing')")
    ).scalar_one()
    if blocked:
        raise RuntimeError("存在未解决的对象写入状态，不能降级")
    op.drop_table("upload_idempotency")
    op.drop_table("upload_object_guard")
