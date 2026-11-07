"""测试用 Mock 网关：确定性向量与可配置回复，支持故障注入。

单元测试一律不调用真实 API；Mock 与真实网关共享错误类型与结果契约。
"""

from __future__ import annotations

import hashlib

import numpy as np

from app.gateways.chat import ChatResult
from app.gateways.embedding import EmbeddingResult, TokenUsage

DEFAULT_MOCK_REPLY = "根据资料 [S1]，员工应在出差结束后 30 日内申请报销。[S1]"


class MockEmbeddingGateway:
    """按 (tenant, template, text) 哈希播种的确定性向量，同输入必同向量。"""

    def __init__(self, dim: int = 1024) -> None:
        self.dim = dim
        self.calls: list[list[str]] = []
        self.failures: list[Exception] = []

    async def embed_texts(
        self,
        texts: list[str],
        *,
        tenant_id: int,
        budget_scope_id: str = "global",
        template_version: str = "v1",
    ) -> EmbeddingResult:
        self.calls.append(list(texts))
        if self.failures:
            raise self.failures.pop(0)
        vectors = np.empty((len(texts), self.dim), dtype=np.float32)
        for i, text in enumerate(texts):
            seed = int.from_bytes(
                hashlib.sha256(f"{tenant_id}|{template_version}|{text}".encode()).digest()[:4],
                "little",
            )
            rng = np.random.default_rng(seed)
            vec = rng.standard_normal(self.dim, dtype=np.float32)
            vectors[i] = vec / np.linalg.norm(vec)
        tokens = sum(len(text) for text in texts)
        return EmbeddingResult(vectors, TokenUsage(prompt_tokens=tokens, total_tokens=tokens))

    async def embed_query(
        self, text: str, *, tenant_id: int, template_version: str = "v1"
    ) -> np.ndarray:
        result = await self.embed_texts([text], tenant_id=tenant_id)
        return result.vectors[0]


class MockChatGateway:
    """返回固定回复并记录调用；可注入失败序列。"""

    def __init__(self, reply: str = DEFAULT_MOCK_REPLY) -> None:
        self.reply = reply
        self.calls: list[dict] = []
        self.failures: list[Exception] = []

    async def generate(
        self,
        *,
        system_prompt: str,
        user_payload: str,
        max_tokens: int | None = None,
        budget_scope_id: str = "global",
    ) -> ChatResult:
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "user_payload": user_payload,
                "max_tokens": max_tokens,
            }
        )
        if self.failures:
            raise self.failures.pop(0)
        prompt_tokens = (len(system_prompt) + len(user_payload)) // 3 + 1
        completion_tokens = max(1, len(self.reply) // 2)
        return ChatResult(
            self.reply,
            "stop",
            TokenUsage(prompt_tokens=prompt_tokens, total_tokens=prompt_tokens + completion_tokens),
        )

    async def generate_stream(
        self,
        *,
        system_prompt: str,
        user_payload: str,
        max_tokens: int | None = None,
        budget_scope_id: str = "global",
    ):
        """按 4 字符分片产出固定回复，模拟真实流式节奏。"""
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "user_payload": user_payload,
                "max_tokens": max_tokens,
            }
        )
        if self.failures:
            raise self.failures.pop(0)
        reply = self.reply
        for i in range(0, len(reply), 4):
            yield {"delta": reply[i : i + 4], "finish_reason": None}
        yield {"delta": "", "finish_reason": "stop"}
