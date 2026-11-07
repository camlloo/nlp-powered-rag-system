"""持久化的对象写入状态与上传幂等状态（由 0006 迁移创建）。

所有对固定对象 key 的修改者必须遵循同一协议：在数据库行锁下将 ready
切换为 writing 并提交，完成对象请求及登记后才转回 ready。writing 不是
租约，不能超时释放：一次超时的远程请求可能仍在运行。
"""

from sqlalchemy import BigInteger, CheckConstraint, Column, String, Table

from app.db.base import Base

metadata = Base.metadata

upload_object_guard = Table(
    "upload_object_guard",
    metadata,
    Column("uri_hash", String(64), primary_key=True),
    Column("uri", String(1000), nullable=False),
    Column("state", String(16), nullable=False),
    Column("token", String(32), nullable=True),
    CheckConstraint("state IN ('ready','writing')", name="ck_upload_object_guard_state"),
    CheckConstraint(
        "(state = 'ready' AND token IS NULL) OR (state = 'writing' AND token IS NOT NULL)",
        name="ck_upload_object_guard_token",
    ),
)

upload_idempotency = Table(
    "upload_idempotency",
    metadata,
    Column("identity_hash", String(64), primary_key=True),
    Column("user_id", BigInteger, nullable=False),
    Column("route", String(200), nullable=False),
    Column("request_key", String(512), nullable=False),
    Column("request_digest", String(64), nullable=False),
    Column("state", String(16), nullable=False),
    Column("token", String(32), nullable=True),
    Column("job_id", BigInteger, nullable=True),
    Column("document_id", BigInteger, nullable=True),
    Column("version_no", BigInteger, nullable=True),
    Column("kb_id", BigInteger, nullable=True),
    CheckConstraint("state IN ('pending','completed')", name="ck_upload_idempotency_state"),
    CheckConstraint(
        "(state = 'pending' AND token IS NOT NULL) OR "
        "(state = 'completed' AND token IS NULL AND job_id IS NOT NULL "
        "AND document_id IS NOT NULL AND version_no IS NOT NULL AND kb_id IS NOT NULL)",
        name="ck_upload_idempotency_result",
    ),
)
