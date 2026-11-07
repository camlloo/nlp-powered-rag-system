"""网关测试共享夹具：假 OpenAI 客户端、异常构造与确定性向量。"""

import asyncio
import hashlib
from types import SimpleNamespace

import numpy as np


def run(coro):
    return asyncio.run(coro)


class FakeAPIError(Exception):
    """模拟 openai API 异常：携带 status_code 与 response.headers（鸭子类型）。"""

    def __init__(self, status_code=None, headers=None):
        super().__init__(f"api error {status_code}")
        self.status_code = status_code
        self.response = SimpleNamespace(headers=headers or {})


def make_vector(text: str, dim: int) -> list[float]:
    """由文本哈希播种的确定性单位向量，用于校验顺序恢复。"""
    seed = int.from_bytes(hashlib.sha256(text.encode()).digest()[:4], "little")
    rng = np.random.default_rng(seed)
    vec = rng.standard_normal(dim)
    return (vec / np.linalg.norm(vec)).tolist()


class FakeEmbeddingsAPI:
    def __init__(self, responder):
        self.calls: list[dict] = []
        self._responder = responder

    async def create(self, **kwargs):
        kwargs["input"] = list(kwargs["input"])
        self.calls.append(kwargs)
        return self._responder(**kwargs)


class FakeEmbeddingClient:
    def __init__(self, responder):
        self.embeddings = FakeEmbeddingsAPI(responder)


class FakeChatCompletions:
    def __init__(self, responder):
        self.calls: list[dict] = []
        self._responder = responder

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._responder(**kwargs)


class FakeChatClient:
    def __init__(self, responder):
        self.chat = SimpleNamespace(completions=FakeChatCompletions(responder))


def embedding_response(inputs: list[str], dim: int = 1024, *, reverse=False, usage_tokens=3):
    data = [
        SimpleNamespace(index=i, embedding=make_vector(text, dim))
        for i, text in enumerate(inputs)
    ]
    if reverse:
        data.reverse()
    return SimpleNamespace(
        data=data,
        usage=SimpleNamespace(
            prompt_tokens=usage_tokens * len(inputs),
            total_tokens=usage_tokens * len(inputs),
        ),
    )


def ok_responder(dim: int = 1024, *, reverse=False):
    def responder(*, model, input, dimensions, encoding_format):
        return embedding_response(input, dim, reverse=reverse)

    return responder


class FlakyResponder:
    """先按序列抛出异常，之后返回正常响应。"""

    def __init__(self, failures: list[Exception], dim: int = 1024):
        self.failures = list(failures)
        self.dim = dim

    def __call__(self, *, model, input, dimensions, encoding_format):
        if self.failures:
            raise self.failures.pop(0)
        return embedding_response(input, self.dim)


def chat_response(*, content="根据资料 [S1]，员工应在 30 日内申请。[S1]", finish_reason="stop"):
    choice = SimpleNamespace(
        finish_reason=finish_reason, message=SimpleNamespace(content=content)
    )
    return SimpleNamespace(
        choices=[choice],
        usage=SimpleNamespace(prompt_tokens=50, total_tokens=80),
    )


def ok_chat_responder(**overrides):
    def responder(**kwargs):
        return chat_response(**overrides)

    return responder
