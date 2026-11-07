"""tests 根级夹具：数据库可用性检查与迁移（集成与评测子树共用）。"""

import pytest
from sqlalchemy import text

from app.db.session import engine


@pytest.fixture(scope="session")
def pg_ready():
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 任何连接失败都视为环境不可用
        pytest.skip(f"PostgreSQL 不可达，跳过集成测试：{exc.__class__.__name__}")

    # 自动执行迁移（幂等），同时覆盖迁移脚本本身的验证
    from alembic import command
    from alembic.config import Config

    command.upgrade(Config("alembic.ini"), "head")
    return True
