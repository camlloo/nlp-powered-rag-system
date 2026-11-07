"""向量模型网关：批量、校验、L2 归一化、缓存与用量记录（实现文档 §6.1/§6.2/§10.1）。

- 批量每次不超过 10 条，按返回项 index 恢复输入顺序，校验缺项/重复；
- 对每个返回向量校验长度、有限浮点值和非零范数，然后 L2 归一化；
- 输入预算按 UTF-8 字节数保守校验，禁止静默截断；
- 缓存键包含租户、模型、维度、输入模板版本与 embedding_input 的 SHA-256，
  正文相同但标题不同不会复用（键含完整输入文本）。
"""

from __future__ import annotations

import hashlib
from collections import OrderedDict
from dataclasses import dataclass

import numpy as np
from openai import AsyncOpenAI

from app.core.config import Settings, get_settings
from app.gateways.budget import GLOBAL_SCOPE, BudgetTracker
from app.gateways.errors import (
    ModelAuthError,
    ModelInputTooLong,
    ModelResponseInvalid,
)
from app.gateways.retry import with_retries

EMBEDDING_BATCH_LIMIT = 10  # 百炼接口单次最多 10 条（实现文档 §2.3）
INPUT_TEMPLATE_VERSION = "v1"  # 与 nlp.pipeline 的 embedding_input 模板对应


@dataclass
class TokenUsage:
    prompt_tokens: int = 0
    total_tokens: int = 0

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
        )


@dataclass
class EmbeddingResult:
    vectors: np.ndarray  # (n, dim) float32，已 L2 归一化
    usage: TokenUsage
    cache_hits: int = 0


def build_client(settings: Settings) -> AsyncOpenAI:
    if not settings.model_configured:
        raise ModelAuthError("MODEL_BASE_URL / DASHSCOPE_API_KEY 未配置")
    return AsyncOpenAI(
        api_key=settings.dashscope_api_key,
        base_url=settings.model_base_url,
        timeout=settings.model_timeout_seconds,
        max_retries=0,  # 统一由模型网关控制重试
    )


class VectorCache:
    """进程内 LRU 缓存；MySQL 是唯一持久化来源，缓存可随时重建。"""

    def __init__(self, max_entries: int) -> None:
        self._max = max_entries
        self._entries: OrderedDict[str, np.ndarray] = OrderedDict()

    def get(self, key: str) -> np.ndarray | None:
        vec = self._entries.get(key)
        if vec is not None:
            self._entries.move_to_end(key)
        return vec

    def put(self, key: str, vec: np.ndarray) -> None:
        self._entries[key] = vec
        self._entries.move_to_end(key)
        while len(self._entries) > self._max:
            self._entries.popitem(last=False)

    def __len__(self) -> int:
        return len(self._entries)


class EmbeddingGateway:
    def __init__(
        self,
        client: AsyncOpenAI,
        *,
        settings: Settings,
        budget: BudgetTracker | None = None,
    ) -> None:
        self._client = client
        self._settings = settings
        self._budget = budget
        self._cache = VectorCache(settings.vector_cache_max_entries)

    @classmethod
    def from_settings(
        cls, settings: Settings | None = None, *, budget: BudgetTracker | None = None
    ) -> EmbeddingGateway:
        settings = settings or get_settings()
        return cls(build_client(settings), settings=settings, budget=budget)

    async def embed_texts(
        self,
        texts: list[str],
        *,
        tenant_id: int,
        budget_scope_id: str = GLOBAL_SCOPE,
        template_version: str = INPUT_TEMPLATE_VERSION,
    ) -> EmbeddingResult:
        """批量生成向量：结果顺序与输入一致，向量已 L2 归一化。"""
        if not texts:
            return EmbeddingResult(
                np.empty((0, self._settings.embedding_dim), dtype=np.float32), TokenUsage()
            )
        for text in texts:
            if not isinstance(text, str) or not text:
                raise ValueError("embedding 输入必须为非空字符串")
            size = len(text.encode("utf-8"))
            if size > self._settings.embedding_input_max_bytes:
                raise ModelInputTooLong(
                    f"输入 {size} 字节超过预算 {self._settings.embedding_input_max_bytes}"
                )

        keys = [self._cache_key(tenant_id, template_version, t) for t in texts]
        key_to_text: dict[str, str] = {}
        pending: list[str] = []  # 未命中缓存的去重键
        cache_hits = 0
        key_to_vec: dict[str, np.ndarray] = {}
        for key, text in zip(keys, texts, strict=True):
            if key in key_to_text:
                continue  # 请求内去重
            key_to_text[key] = text
            cached = self._cache.get(key)
            if cached is not None:
                key_to_vec[key] = cached
                cache_hits += 1
            else:
                pending.append(key)

        if self._budget and pending:
            estimate = sum(len(key_to_text[k].encode("utf-8")) for k in pending) // 3 + len(pending)
            self._budget.ensure_within(
                estimated_tokens=max(1, estimate), scope_id=budget_scope_id
            )

        usage = TokenUsage()
        for start in range(0, len(pending), EMBEDDING_BATCH_LIMIT):
            batch_keys = pending[start : start + EMBEDDING_BATCH_LIMIT]
            batch_texts = [key_to_text[k] for k in batch_keys]
            vectors, batch_usage = await with_retries(
                lambda batch=batch_texts: self._call(batch),
                settings=self._settings,
            )
            usage = usage + batch_usage
            for key, vec in zip(batch_keys, vectors, strict=True):
                key_to_vec[key] = vec
                self._cache.put(key, vec)
            if self._budget:
                self._budget.add(tokens=batch_usage.total_tokens, scope_id=budget_scope_id)

        matrix = np.stack([key_to_vec[k] for k in keys]).astype(np.float32)
        return EmbeddingResult(matrix, usage, cache_hits)

    async def embed_query(
        self, text: str, *, tenant_id: int, template_version: str = INPUT_TEMPLATE_VERSION
    ) -> np.ndarray:
        """查询向量；超时/不可用时抛 EmbeddingUnavailable，检索侧降级 BM25。"""
        result = await self.embed_texts(
            [text], tenant_id=tenant_id, template_version=template_version
        )
        return result.vectors[0]

    def _cache_key(self, tenant_id: int, template_version: str, text: str) -> str:
        raw = (
            f"{tenant_id}|{self._settings.embedding_model}|"
            f"{self._settings.embedding_dim}|{template_version}|{text}"
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    async def _call(self, batch: list[str]) -> tuple[np.ndarray, TokenUsage]:
        dim = self._settings.embedding_dim
        response = await self._client.embeddings.create(
            model=self._settings.embedding_model,
            input=list(batch),
            dimensions=dim,
            encoding_format="float",
        )
        items = sorted(response.data, key=lambda x: x.index)
        indices = [item.index for item in items]
        if indices != list(range(len(batch))):
            raise ModelResponseInvalid(f"embedding 返回索引缺项或重复：{indices}")
        arr = np.asarray([item.embedding for item in items], dtype=np.float32)
        if arr.shape != (len(batch), dim):
            raise ModelResponseInvalid(f"embedding 维度不符：{arr.shape} != ({len(batch)}, {dim})")
        if not np.isfinite(arr).all():
            raise ModelResponseInvalid("embedding 含非有限值")
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        if not np.isfinite(norms).all() or (norms <= 0).any():
            raise ModelResponseInvalid("embedding 范数无效")
        usage = TokenUsage(
            prompt_tokens=getattr(response.usage, "prompt_tokens", 0) or 0,
            total_tokens=getattr(response.usage, "total_tokens", 0) or 0,
        )
        return arr / norms, usage
