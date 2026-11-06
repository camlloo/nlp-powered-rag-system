"""初始化种子数据：默认知识库与预留成员关系（幂等，可重复执行）。"""

import sys

from sqlalchemy import select

from app.core.config import get_settings
from app.db.models import KbCollection, KbMember
from app.db.session import SessionLocal


def main() -> int:
    settings = get_settings()
    with SessionLocal() as session:
        existing = session.scalar(
            select(KbCollection)
            .where(KbCollection.tenant_id == settings.default_tenant_id)
            .order_by(KbCollection.id)
            .limit(1)
        )
        if existing is not None:
            print(f"已存在知识库 kb_id={existing.id} name={existing.name!r}，跳过")
            return 0

        kb = KbCollection(tenant_id=settings.default_tenant_id, name="示例知识库")
        session.add(kb)
        session.flush()
        # kb_member 为预留表：写入 owner 关系便于后期 RBAC 启用，首期不做业务校验
        session.add(
            KbMember(kb_id=kb.id, user_id=settings.default_user_id, role="owner")
        )
        session.commit()
        print(
            f"已创建知识库 kb_id={kb.id} name='示例知识库' "
            f"tenant_id={settings.default_tenant_id}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
