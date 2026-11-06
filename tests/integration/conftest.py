"""集成测试夹具：无数据库时自动跳过；每个用例在独立事务中运行并回滚。"""

import pytest
from sqlalchemy import text

from app.db.session import SessionLocal, engine


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


@pytest.fixture
def db_session(pg_ready):
    connection = engine.connect()
    transaction = connection.begin()
    session = SessionLocal(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()
