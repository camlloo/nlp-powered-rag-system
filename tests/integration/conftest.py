"""集成测试夹具：db_session（独立事务回滚）与 indexed_kb（预构建知识库）。

pg_ready 定义在 tests/conftest.py（与评测子树共用）。

indexed_kb 种入 fake 桶时同步登记 upload_object_guard ready 行：协议内
对象才可被 cleanup 物理清除，缺行则代表迁移前遗留、cleanup 只保留。
"""

import pytest

from app.db.session import SessionLocal


@pytest.fixture
def db_session(pg_ready):
    connection = pg_ready.connect()
    transaction = connection.begin()
    session = SessionLocal(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def indexed_kb(pg_ready, test_settings, fake_minio):
    """建库 + 上传两个文本文件 + 真实 builder 构建快照，yield (kb_id, settings)。"""
    import asyncio

    from app.db.models import KbCollection, KbDocument, KbDocumentVersion
    from app.gateways.mock import MockEmbeddingGateway
    from app.indexing.snapshot import IndexBuilder
    from tests.integration.helpers import cleanup_kb, create_kb, seed_object_guard

    kb_id = create_kb()
    settings = test_settings
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
        uri, digest = fake_minio.seed(settings, name, content)
        with SessionLocal() as session:
            document = KbDocument(kb_id=kb_id, title=name.rsplit(".", 1)[0])
            session.add(document)
            session.flush()
            version = KbDocumentVersion(
                kb_id=kb_id,
                document_id=document.id,
                version_no=1,
                title=document.title,
                file_sha256=digest,
                storage_uri=uri,
                parse_status="pending",
            )
            session.add(version)
            seed_object_guard(session, uri)
            kb = session.get(KbCollection, kb_id)
            kb.data_revision += 1
            session.commit()
            doc_version_id = version.id
        asyncio.run(builder.build(kb_id=kb_id, new_doc_version_id=doc_version_id))
    yield kb_id, settings
    cleanup_kb(kb_id)
