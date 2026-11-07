"""单一主空间：合并既有知识库到「主空间」，组织/个人归属由文档级字段承载。

产品层不再出现知识库概念：所有存活文档并入 tenant 1 的「主空间」，
组织/个人归属与公开/私有完全由 kb_document 的 org_tag_id / visibility /
uploader_user_id 承载（检索过滤逻辑不变）。旧知识库的索引/块/任务为
运行时数据，直接清除；并入文档排队一条全量重建任务（worker 运行后重建）。

- uq_kb_collection_tenant_name 支撑主空间并发安全的 get-or-create；
- 复合外键 fk_kb_document_version_kb_id_kb_document 不可延迟，搬移期间摘除后重建；
- qa_* 审计表无外键，保留原值（kb_id 成为历史值）；
- 数据合并不可逆：downgrade 仅移除本次新增的约束。
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

MAIN_SPACE_NAME = "主空间"


def _main_space_id(bind) -> int:
    """按名取主空间，不存在则创建（迁移期无并发，直接 SELECT/INSERT）。"""
    existing = bind.execute(
        sa.text("SELECT id FROM kb_collection WHERE tenant_id = 1 AND name = :n LIMIT 1"),
        {"n": MAIN_SPACE_NAME},
    ).scalar()
    if existing is not None:
        return int(existing)
    return int(
        bind.execute(
            sa.text(
                "INSERT INTO kb_collection (tenant_id, name) VALUES (1, :n) RETURNING id"
            ),
            {"n": MAIN_SPACE_NAME},
        ).scalar_one()
    )


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_kb_collection_tenant_name", "kb_collection", ["tenant_id", "name"]
    )
    bind = op.get_bind()
    main_id = _main_space_id(bind)

    # 版本表复合外键不可延迟：先摘除，搬移完再重建
    op.drop_constraint(
        "fk_kb_document_version_kb_id_kb_document", "kb_document_version", type_="foreignkey"
    )
    try:
        # 块与向量必须最先清除：kb_chunk 对文档与版本均有复合外键，
        # 搬移 kb_id 会使旧引用立即失效（并入文档稍后由 reindex 重建全部块）
        bind.execute(
            sa.text(
                "DELETE FROM kb_embedding WHERE chunk_id IN "
                "(SELECT id FROM kb_chunk WHERE kb_id != :m)"
            ),
            {"m": main_id},
        )
        bind.execute(sa.text("DELETE FROM kb_chunk WHERE kb_id != :m"), {"m": main_id})
        # 存活文档与其版本并入主空间（软删文档随旧库清除，不迁移）
        bind.execute(
            sa.text(
                "UPDATE kb_document SET kb_id = :m "
                "WHERE kb_id != :m AND deleted_at IS NULL"
            ),
            {"m": main_id},
        )
        bind.execute(
            sa.text(
                "UPDATE kb_document_version SET kb_id = :m "
                "WHERE kb_id != :m AND document_id IN "
                "(SELECT id FROM kb_document WHERE kb_id = :m)"
            ),
            {"m": main_id},
        )
        # 旧库运行时数据按引用顺序清除（ingest_job.kb_id 有外键必须先删）
        bind.execute(sa.text("DELETE FROM ingest_job WHERE kb_id != :m"), {"m": main_id})
        bind.execute(sa.text("DELETE FROM kb_index WHERE kb_id != :m"), {"m": main_id})
        bind.execute(
            sa.text("DELETE FROM kb_document_version WHERE kb_id != :m"), {"m": main_id}
        )
        bind.execute(sa.text("DELETE FROM kb_document WHERE kb_id != :m"), {"m": main_id})
        bind.execute(sa.text("DELETE FROM kb_member WHERE kb_id != :m"), {"m": main_id})
        bind.execute(sa.text("DELETE FROM kb_collection WHERE id != :m"), {"m": main_id})
        # 主空间清单已变化：修订号递增使一切缓存失效；索引归零等待重建
        bind.execute(
            sa.text(
                "UPDATE kb_collection SET active_index_id = NULL, "
                "data_revision = data_revision + 1 WHERE id = :m"
            ),
            {"m": main_id},
        )
        # 排队全量重建：worker 运行即重新解析并入文档并发布新快照
        bind.execute(
            sa.text(
                "INSERT INTO ingest_job (kb_id, job_type, status, payload_json) "
                "VALUES (:m, 'reindex', 'queued', '{}')"
            ),
            {"m": main_id},
        )
    finally:
        op.create_foreign_key(
            "fk_kb_document_version_kb_id_kb_document",
            "kb_document_version",
            "kb_document",
            ["kb_id", "document_id"],
            ["kb_id", "id"],
        )


def downgrade() -> None:
    op.drop_constraint("uq_kb_collection_tenant_name", "kb_collection", type_="unique")
