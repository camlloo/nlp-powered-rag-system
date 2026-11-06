"""Alembic 环境：连接串取自应用配置，元数据来自 app.db.models。"""

import os

from alembic import context
from sqlalchemy import create_engine, pool

from app.core.config import get_settings
from app.db import models  # noqa: F401  确保模型注册到 metadata
from app.db.base import Base

config = context.config
target_metadata = Base.metadata


def _url() -> str:
    override = os.getenv("ALEMBIC_DATABASE_URL")
    if override:
        return override
    return get_settings().sync_dsn


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool, connect_args={"connect_timeout": 10})
    try:
        with engine.connect() as connection:
            context.configure(connection=connection, target_metadata=target_metadata)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
