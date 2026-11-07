"""网关实例工厂：进程内单例，保证向量缓存跨请求复用。

未配置模型凭证且处于开发环境时回退 Mock（便于本地端到端联调）；
生产环境凭证缺失将直接抛 ModelAuthError，不静默降级。
"""

from __future__ import annotations

import logging
from functools import lru_cache

from app.core.config import get_settings
from app.gateways.budget import BudgetTracker
from app.gateways.chat import ChatGateway
from app.gateways.embedding import EmbeddingGateway
from app.gateways.errors import ModelAuthError
from app.gateways.mock import MockChatGateway, MockEmbeddingGateway

logger = logging.getLogger(__name__)


@lru_cache
def get_budget() -> BudgetTracker:
    return BudgetTracker(get_settings())


@lru_cache
def get_embedding_gateway() -> EmbeddingGateway | MockEmbeddingGateway:
    settings = get_settings()
    if settings.model_configured:
        return EmbeddingGateway.from_settings(settings, budget=get_budget())
    if settings.app_env == "development":
        logger.warning("模型凭证未配置，开发环境使用 Mock 向量网关")
        return MockEmbeddingGateway(dim=settings.embedding_dim)
    raise ModelAuthError("MODEL_BASE_URL / DASHSCOPE_API_KEY 未配置")


@lru_cache
def get_chat_gateway() -> ChatGateway | MockChatGateway:
    settings = get_settings()
    if settings.model_configured:
        return ChatGateway.from_settings(settings, budget=get_budget())
    if settings.app_env == "development":
        logger.warning("模型凭证未配置，开发环境使用 Mock 问答网关")
        return MockChatGateway()
    raise ModelAuthError("MODEL_BASE_URL / DASHSCOPE_API_KEY 未配置")
