"""离线验证集成测试用的对象存储与默认数据库防护。"""

import pytest

from app.core.config import Settings
from app.db.session import engine
from app.parsers.intake import load_upload
from app.storage import (
    MinioStorage,
    StorageConfigurationError,
    StorageNotFound,
    build_object_key,
    object_uri,
)
from tests.conftest import pg_ready
from tests.support.fake_minio import TEST_BUCKET


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("离线隔离测试禁止连接网络")

    monkeypatch.setattr("socket.socket.connect", forbidden)
    monkeypatch.setattr("socket.socket.connect_ex", forbidden)
    monkeypatch.setattr("socket.create_connection", forbidden)


def test_fake_minio_put_get_delete(fake_minio, test_settings):
    uri, digest = fake_minio.seed(test_settings, "中文名称.txt", b"hello")
    assert len(digest) == 32
    assert fake_minio.contains(uri)
    assert load_upload(uri, test_settings) == b"hello"
    fake_minio.storage(test_settings).delete(uri)
    assert not fake_minio.contains(uri)
    with pytest.raises(StorageNotFound):
        load_upload(uri, test_settings)
    assert [event[0] for event in fake_minio.events] == ["put", "get", "delete", "get"]


def test_fake_rejects_non_test_bucket(fake_minio, test_settings):
    settings = test_settings.model_copy(update={"minio_bucket": "user-bucket"})
    with pytest.raises(AssertionError, match="fake bucket"):
        fake_minio.storage(settings)
    with pytest.raises(ValueError):
        fake_minio.contains(object_uri("user-bucket", build_object_key(1, "a.txt")))
    assert fake_minio.objects == {}


def test_fake_is_isolated_between_tests(fake_minio, test_settings):
    assert fake_minio.objects == {}
    assert fake_minio.events == []
    assert test_settings.minio_bucket == TEST_BUCKET


def test_fake_fixture_blocks_real_client_even_with_credentials(fake_minio, test_settings):
    settings = test_settings.model_copy(update={
        "minio_access_key": "test", "minio_secret_key": "test"
    })
    with pytest.raises(StorageConfigurationError):
        MinioStorage(settings)
    assert fake_minio.objects == {}


def test_default_engine_fails_before_network():
    with pytest.raises(RuntimeError, match="禁止连接"):
        engine.connect()


def test_settings_do_not_load_dotenv():
    assert Settings.model_config["env_file"] is None


def test_library_connections_work_without_construction_of_real_client(fake_minio, test_settings):
    from app.api import documents
    from app.parsers import intake

    assert documents.MinioStorage(test_settings)._client is fake_minio
    assert intake.MinioStorage(test_settings)._client is fake_minio
    assert documents.MinioStorage(test_settings).safe_mutation_no_retry


@pytest.mark.parametrize("url", [
    "postgresql+psycopg://user:pw@127.0.0.1:5432/nlp_rag",
    "postgresql+psycopg://user:pw@localhost:5432/nlp_rag_test_0123456789abcdef0123456789abcdef",
    "postgresql+psycopg://user:pw@127.0.0.1:5432/"
    "nlp_rag_test_0123456789abcdef0123456789abcdef?hostaddr=192.0.2.1",
])
def test_database_rejects_untrusted_url_before_connect(monkeypatch, url):
    monkeypatch.setenv("NLP_RAG_TEST_DATABASE_URL", url)
    with pytest.raises(pytest.fail.Exception, match="隔离测试库 URL"):
        next(pg_ready.__wrapped__())
