"""应用配置：pydantic-settings 加载 .env，启动时强校验关键参数。

实现文档 §5.4：DDL 固定 1024 维，应用必须在启动时校验 EMBEDDING_DIM=1024，
改变维度应迁移到新向量表并调整约束，不允许只改环境变量。
"""

from functools import lru_cache
from urllib.parse import quote

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 实现文档附录 A：DDL 与启动校验共同固定维度
REQUIRED_EMBEDDING_DIM = 1024

# 首期仅支持路径 A（应用层精确检索）；pgvector_index 为 B 路径预留
SUPPORTED_VECTOR_BACKENDS = {"application_exact", "pgvector_index"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Traditional NLP RAG"
    app_env: str = "development"
    api_prefix: str = "/api/v1"
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "INFO"

    # PostgreSQL：开发环境单账号直连；生产部署时再按实现文档 §11 分权
    pg_host: str = "127.0.0.1"
    pg_port: int = 5433
    pg_database: str = "nlp_rag"
    pg_user: str = "postgres"
    pg_password: str = "postgres"

    # 首期静态 API Token；后期以 JWT RBAC 替换，授权校验点不变
    api_token: str = ""
    default_tenant_id: int = 1
    default_user_id: int = 1
    cors_origins: str = "http://localhost:5173"

    # 模型网关配置；模型相关校验延迟到网关调用时进行
    model_base_url: str = ""
    dashscope_api_key: str = Field(repr=False, default="")
    embedding_model: str = "text-embedding-v4"
    embedding_dim: int = REQUIRED_EMBEDDING_DIM
    chat_model: str = "qwen3.8-flash"
    model_timeout_seconds: float = 30.0
    vector_backend: str = "application_exact"

    chunk_max_chars: int = 700
    chunk_overlap_chars: int = 100
    max_upload_bytes: int = 20 * 1024 * 1024

    @field_validator("embedding_dim")
    @classmethod
    def _check_dim(cls, value: int) -> int:
        if value != REQUIRED_EMBEDDING_DIM:
            raise ValueError(
                f"EMBEDDING_DIM 必须为 {REQUIRED_EMBEDDING_DIM}（与 DDL 约束一致），"
                "变更维度需要新向量表迁移，不允许只改环境变量"
            )
        return value

    @field_validator("vector_backend")
    @classmethod
    def _check_backend(cls, value: str) -> str:
        if value not in SUPPORTED_VECTOR_BACKENDS:
            allowed = ", ".join(sorted(SUPPORTED_VECTOR_BACKENDS))
            raise ValueError(f"VECTOR_BACKEND 仅支持：{allowed}")
        return value

    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]

    @property
    def model_configured(self) -> bool:
        return bool(self.model_base_url and self.dashscope_api_key)

    def _dsn(self, user: str, password: str) -> str:
        # safe=""：userinfo 中的空格与特殊字符一律百分号编码
        return (
            "postgresql+psycopg://"
            f"{quote(user, safe='')}:{quote(password, safe='')}"
            f"@{self.pg_host}:{self.pg_port}/{self.pg_database}"
        )

    @property
    def sync_dsn(self) -> str:
        """应用与迁移共用的 DSN（开发环境单账号）。"""
        return self._dsn(self.pg_user, self.pg_password)


@lru_cache
def get_settings() -> Settings:
    return Settings()
