"""主空间：产品层不再出现知识库概念，内部以单一 kb_collection 行承载全部文档。

组织/个人归属与公开/私有由 kb_document 的 org_tag_id / visibility /
uploader_user_id 承载，检索过滤逻辑不依赖知识库；主空间只是快照索引、
运行时缓存与 worker 任务（均以 kb_id 为键）的挂载点。
"""

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import Conflict
from app.db.models import KbCollection

MAIN_SPACE_NAME = "主空间"  # 保留名：0004 迁移与本地运行共用（POST /kbs 拒绝占用）


def get_or_create_main_space(session: Session) -> KbCollection:
    """按名取主空间；并发下依靠 uq_kb_collection_tenant_name 幂等创建。

    创建即提交（读路径如问答也要能拿到主空间，请求级会话不会自动提交）。
    """
    tenant_id = get_settings().default_tenant_id
    where = (
        KbCollection.tenant_id == tenant_id,
        KbCollection.name == MAIN_SPACE_NAME,
        KbCollection.deleted_at.is_(None),
    )
    kb = session.scalar(select(KbCollection).where(*where))
    if kb is not None:
        return kb
    session.execute(
        insert(KbCollection)
        .values(tenant_id=tenant_id, name=MAIN_SPACE_NAME)
        .on_conflict_do_nothing(index_elements=["tenant_id", "name"])
    )
    kb = session.scalar(select(KbCollection).where(*where))
    if kb is None:
        raise Conflict("主空间创建失败，请重试")
    session.commit()
    return kb
