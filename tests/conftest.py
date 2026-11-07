"""隔离测试配置；集成/评测仅可使用显式提供的空白专用 PostgreSQL。"""

import os
import re
from functools import lru_cache
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url

from app.core import config as app_config

# 在测试模块收集和 app.db.session 导入之前禁用 dotenv，绝不读取项目 .env。
app_config.Settings.model_config["env_file"] = None
# 显式传入每个默认值，防止进程环境变量污染集成测试配置。
_defaults = {
    name: field.get_default(call_default_factory=True)
    for name, field in app_config.Settings.model_fields.items()
}
_defaults.update(
    _env_file=None,
    pg_host="127.0.0.1",
    pg_port=1,
    pg_database="nlp_rag_tests_disabled",
    pg_user="test-only",
    pg_password="test-only",
    api_token="isolated-test-token",
    jwt_secret="isolated-test-jwt-secret",
    model_base_url="",
    dashscope_api_key="",
    minio_endpoint="127.0.0.1:1",
    minio_bucket="nlp-rag-isolated-test",
    minio_access_key="",
    minio_secret_key="",
)
_TEST_SETTINGS = app_config.Settings(**_defaults)


@lru_cache
def _get_test_settings():
    return _TEST_SETTINGS


app_config.get_settings = _get_test_settings

# 即便调用者忘记 pg_ready，应用原始引擎也不会发起连接。
from app.db import session as db  # noqa: E402


@event.listens_for(db.engine, "do_connect")
def _deny_unprepared_database(*args, **kwargs):
    raise RuntimeError("测试数据库尚未隔离初始化，禁止连接")


@pytest.fixture
def test_settings():
    return _TEST_SETTINGS.model_copy(deep=True)


@pytest.fixture
def fake_minio(monkeypatch):
    from app.api import documents
    from app.parsers import intake
    from tests.support.fake_minio import FakeMinio

    fake = FakeMinio()

    def forbidden_client(*args, **kwargs):
        raise AssertionError("集成/评测测试禁止构造真实 MinIO 客户端")

    monkeypatch.setattr("app.storage.Minio", forbidden_client)
    monkeypatch.setattr("app.storage._NoRetryMinio", forbidden_client)
    monkeypatch.setattr(documents, "MinioStorage", fake.storage)
    monkeypatch.setattr(intake, "MinioStorage", fake.storage)
    return fake


@pytest.fixture(autouse=True)
def _isolate_integration_storage(request):
    # 其他集成测试也会走上传路径；统一注入，避免遗漏真实客户端。
    path = Path(str(request.node.path))
    if any(part in {"integration", "evaluation"} for part in path.parts):
        request.getfixturevalue("fake_minio")


@pytest.fixture(autouse=True)
def _reset_test_database(request):
    # 迁移 0004 会创建主空间和 queued reindex；不同测试也会提交任务与 guard。
    # 仅在 pg_ready 已选择全新专用库之后重置，避免任务被其他测试领取。
    if "pg_ready" not in request.fixturenames:
        return
    engine = request.getfixturevalue("pg_ready")
    # 主键每次重置；进程级索引缓存不能复用上个测试的同号知识库。
    from app.indexing.cache import get_index_cache

    get_index_cache.cache_clear()
    with engine.begin() as conn:
        names = conn.execute(text("""
            SELECT tablename FROM pg_tables
            WHERE schemaname = 'public' AND tablename <> 'alembic_version'
            ORDER BY tablename
        """)).scalars().all()
        quote = conn.dialect.identifier_preparer.quote
        if names:
            tables = ", ".join(f"public.{quote(name)}" for name in names)
            conn.exec_driver_sql(f"TRUNCATE TABLE {tables} RESTART IDENTITY CASCADE")
        conn.execute(text("INSERT INTO kb_collection (tenant_id, name) VALUES (1, :name)"),
                     {"name": "主空间"})


@pytest.fixture(scope="session")
def pg_ready():
    """不自动创建或清空数据库，只接受显式指定且完全空白的专用测试库。

    NLP_RAG_TEST_DATABASE_URL 需为本机 PostgreSQL 的
    postgresql+psycopg://user:password@127.0.0.1:port/nlp_rag_test_<32位hex>。
    每次运行须使用新的空库；不指定时跳过，不能退回开发库。
    """
    raw = os.environ.get("NLP_RAG_TEST_DATABASE_URL")
    if not raw:
        pytest.skip("未显式指定空白隔离测试库 NLP_RAG_TEST_DATABASE_URL")
    try:
        url = make_url(raw)
        valid = (
            url.drivername == "postgresql+psycopg"
            and url.host == "127.0.0.1"
            and url.port is not None
            and 1 <= url.port <= 65535
            and bool(url.username)
            and url.password is not None
            and re.fullmatch(r"nlp_rag_test_[0-9a-f]{32}", url.database or "")
            and not url.query
        )
    except Exception:
        valid = False
    if not valid:
        pytest.fail("隔离测试库 URL 不符合本机、随机专用库名和无附加参数的要求")

    # libpq 的 PGHOSTADDR、PGSERVICE、PGOPTIONS 等也能改变最终连接目标或配置。
    # 在整个数据库测试会话中移除这些继承值，并确保所有路径使用本机地址。
    pg_env = pytest.MonkeyPatch()
    for name in tuple(os.environ):
        if name.upper().startswith("PG"):
            pg_env.delenv(name, raising=False)
    safe_url = url.update_query_dict({"hostaddr": "127.0.0.1"})
    engine = create_engine(safe_url, pool_pre_ping=True, connect_args={"connect_timeout": 3})
    lock = None
    original_engine = db.engine
    try:
        try:
            lock = engine.connect()
        except Exception as exc:
            pytest.skip(f"隔离测试库不可达：{type(exc).__name__}")
        if lock.scalar(text("SELECT current_database()")) != url.database:
            pytest.fail("连接的数据库不是指定的隔离测试库")
        if not lock.scalar(text("SELECT pg_try_advisory_lock(720987601, 92371645)")):
            pytest.fail("隔离测试库正在被其他测试使用")
        nonempty = lock.scalar(text("""
            SELECT EXISTS (
                SELECT 1 FROM pg_namespace n
                WHERE n.nspname NOT LIKE 'pg_%'
                  AND n.nspname <> 'information_schema'
                  AND (n.nspname <> 'public' OR EXISTS (
                      SELECT 1 FROM pg_class c WHERE c.relnamespace = n.oid
                  ))
            ) OR EXISTS (SELECT 1 FROM pg_extension WHERE extname <> 'plpgsql')
        """))
        if nonempty:
            pytest.fail("隔离测试库不是空库；拒绝执行迁移或清理")
        lock.commit()

        from alembic import command
        from alembic.config import Config

        project = Path(__file__).resolve().parents[1]
        alembic_config = Config(str(project / "alembic.ini"))
        alembic_config.set_main_option("script_location", str(project / "migrations"))
        # migrations/env.py 优先读取此变量，不能继承外部的 Alembic URL。
        with pytest.MonkeyPatch.context() as patch:
            patch.setenv("ALEMBIC_DATABASE_URL", safe_url.render_as_string(hide_password=False))
            command.upgrade(alembic_config, "head")
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0006"
            assert conn.scalar(text("SELECT to_regclass('public.upload_object_guard')"))
            assert conn.scalar(text("SELECT to_regclass('public.upload_idempotency')"))
        db.SessionLocal.configure(bind=engine)
        db.engine = engine
        yield engine
    finally:
        db.SessionLocal.configure(bind=original_engine)
        db.engine = original_engine
        if lock is not None:
            lock.close()
        engine.dispose()
        pg_env.undo()
