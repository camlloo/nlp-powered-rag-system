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

    # 模型网关：重试/预算/输入预算/缓存（实现文档 §6.2；SDK 重试关闭，网关统一控制）
    retry_max_attempts: int = 3
    retry_base_delay_seconds: float = 0.5
    retry_max_delay_seconds: float = 8.0
    embedding_input_max_bytes: int = 16384
    model_daily_token_budget: int = 1_000_000  # 0 表示不限
    model_per_file_token_budget: int = 100_000  # 0 表示不限
    chat_max_output_tokens: int = 1200
    vector_cache_max_entries: int = 10000

    # 索引与 worker（实现文档 §5.2/§9.4/§13.1）
    storage_dir: str = "data"  # 原始文件根目录，storage_uri 形如 uploads/<随机名>
    worker_lease_seconds: int = 300
    worker_poll_interval_seconds: float = 2.0
    index_cache_budget_mb: int = 256  # 单进程索引缓存起始预算（向量+文本+词项）

    chunk_max_chars: int = 700
    chunk_overlap_chars: int = 100
    chunk_hard_max_chars: int = 1000
    chunk_min_chars: int = 120
    max_upload_bytes: int = 20 * 1024 * 1024
    # 向量输入前缀（文档标题+标题路径）长度上限（实现文档 §4.5/§6.1）
    embedding_title_prefix_chars: int = 100

    # 解析限制与回退（实现文档 §4.1）；解析超时 120s 由 worker 层执行
    fallback_encoding: str = "gb18030"
    max_pdf_pages: int = 300
    docx_zip_guard_bytes: int = 200 * 1024 * 1024
    # PDF 质量阈值：低质量转 needs_review，不发布索引（阈值需用真实样本校准）
    pdf_min_readable_ratio: float = 0.7
    pdf_max_abnormal_ratio: float = 0.1
    pdf_min_page_chars: int = 20

    @field_validator("chunk_hard_max_chars")
    @classmethod
    def _check_chunk_bounds(cls, value: int, info) -> int:
        max_chars = info.data.get("chunk_max_chars")
        if max_chars is not None and value < max_chars:
            raise ValueError("CHUNK_HARD_MAX_CHARS 不能小于 CHUNK_MAX_CHARS")
        return value

    @field_validator("chunk_min_chars")
    @classmethod
    def _check_chunk_min(cls, value: int, info) -> int:
        max_chars = info.data.get("chunk_max_chars")
        if max_chars is not None and value >= max_chars:
            raise ValueError("CHUNK_MIN_CHARS 必须小于 CHUNK_MAX_CHARS")
        return value

    @field_validator("retry_max_attempts")
    @classmethod
    def _check_retry_attempts(cls, value: int) -> int:
        if value < 1:
            raise ValueError("RETRY_MAX_ATTEMPTS 至少为 1")
        return value

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
