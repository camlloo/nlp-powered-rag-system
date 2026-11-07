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


@pytest.fixture
def indexed_kb(pg_ready, tmp_path):
    """建库 + 上传两个文本文件 + 真实 builder 构建快照，yield (kb_id, settings)。"""
    import asyncio

    from app.core.config import Settings
    from app.db.models import KbCollection, KbDocument, KbDocumentVersion
    from app.gateways.mock import MockEmbeddingGateway
    from app.indexing.snapshot import IndexBuilder
    from app.parsers.intake import save_upload
    from tests.integration.helpers import cleanup_kb, create_kb

    kb_id = create_kb()
    settings = Settings(storage_dir=str(tmp_path))
    builder = IndexBuilder(
        SessionLocal, MockEmbeddingGateway(dim=1024), settings, worker_id="fixture-worker"
    )
    files = [
        (
            "报销制度.txt",
            "第三章 费用报销。员工应在出差结束后 30 日内申请。交通费凭有效票据报销。".encode(),
        ),
        (
            "考勤制度.txt",
            "第四章 考勤。员工应当在工作日 09:00 前签到。迟到按旷工处理。".encode(),
        ),
    ]
    for name, content in files:
        uri, sha_hex = save_upload(content, "txt", upload_dir=settings.storage_dir + "/uploads")
        with SessionLocal() as session:
            document = KbDocument(kb_id=kb_id, title=name.rsplit(".", 1)[0])
            session.add(document)
            session.flush()
            version = KbDocumentVersion(
                kb_id=kb_id,
                document_id=document.id,
                version_no=1,
                title=document.title,
                file_sha256=bytes.fromhex(sha_hex),
                storage_uri=uri,
                parse_status="pending",
            )
            session.add(version)
            kb = session.get(KbCollection, kb_id)
            kb.data_revision += 1
            session.commit()
            doc_version_id = version.id
        asyncio.run(builder.build(kb_id=kb_id, new_doc_version_id=doc_version_id))
    yield kb_id, settings
    cleanup_kb(kb_id)
