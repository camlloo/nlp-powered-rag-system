"""配置加载与启动校验单测（不依赖数据库）。"""

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_default_dim_is_1024():
    settings = Settings()
    assert settings.embedding_dim == 1024
    assert settings.sync_dsn.startswith("postgresql+psycopg://")


def test_dim_must_be_1024():
    with pytest.raises(ValidationError, match="1024"):
        Settings(embedding_dim=768)


def test_vector_backend_rejects_unknown():
    with pytest.raises(ValidationError, match="VECTOR_BACKEND"):
        Settings(vector_backend="heatwave")


def test_dsn_quotes_credentials():
    dsn = Settings(pg_user="a b", pg_password="p@ss:1", pg_port=5433).sync_dsn
    assert "a%20b:p%40ss%3A1@127.0.0.1:5433/nlp_rag" in dsn


def test_cors_origin_list():
    assert Settings(cors_origins="http://a, http://b").cors_origin_list == [
        "http://a",
        "http://b",
    ]
