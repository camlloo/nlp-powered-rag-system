"""向量网关单测：顺序恢复、批量、协议校验、归一化、缓存与预算（实现文档 §14.3 第 4 条）。"""

import numpy as np
import pytest

from app.core.config import Settings
from app.gateways.budget import BudgetTracker
from app.gateways.embedding import EmbeddingGateway
from app.gateways.errors import (
    ModelBudgetExceeded,
    ModelInputTooLong,
    ModelResponseInvalid,
)
from tests.unit.gateways.gateway_helpers import (
    FakeAPIError,
    FakeEmbeddingClient,
    FlakyResponder,
    make_vector,
    ok_responder,
    run,
)

DIM = 1024


def make_gateway(responder, settings=None, budget=None) -> EmbeddingGateway:
    settings = settings or Settings()
    return EmbeddingGateway(FakeEmbeddingClient(responder), settings=settings, budget=budget)


def test_order_restored_from_reversed_response():
    texts = ["甲段落", "乙段落", "丙段落"]
    gateway = make_gateway(ok_responder(DIM, reverse=True))
    result = run(gateway.embed_texts(texts, tenant_id=1))
    for i, text in enumerate(texts):
        assert np.allclose(result.vectors[i], np.asarray(make_vector(text, DIM)), atol=1e-6)


def test_batches_limited_to_ten():
    texts = [f"第{i}块内容" for i in range(25)]
    gateway = make_gateway(ok_responder(DIM))
    result = run(gateway.embed_texts(texts, tenant_id=1))
    sizes = [len(call["input"]) for call in gateway._client.embeddings.calls]
    assert sizes == [10, 10, 5]
    assert result.vectors.shape == (25, DIM)
    assert result.usage.total_tokens == 3 * 25


def test_missing_index_rejected():
    def responder(**kwargs):
        response = ok_responder(DIM)(**kwargs)
        response.data.pop(1)  # 缺项
        return response

    gateway = make_gateway(responder)
    with pytest.raises(ModelResponseInvalid, match="索引"):
        run(gateway.embed_texts(["一", "二", "三"], tenant_id=1))


def test_duplicate_index_rejected():
    def responder(**kwargs):
        response = ok_responder(DIM)(**kwargs)
        response.data[1].index = 0  # 重复
        return response

    gateway = make_gateway(responder)
    with pytest.raises(ModelResponseInvalid, match="索引"):
        run(gateway.embed_texts(["一", "二"], tenant_id=1))


def test_wrong_dimension_rejected():
    def responder(**kwargs):
        return ok_responder(DIM - 1)(**kwargs)

    gateway = make_gateway(responder)
    with pytest.raises(ModelResponseInvalid, match="维度"):
        run(gateway.embed_texts(["一"], tenant_id=1))


def test_nan_rejected():
    def responder(**kwargs):
        response = ok_responder(DIM)(**kwargs)
        response.data[0].embedding[0] = float("nan")
        return response

    gateway = make_gateway(responder)
    with pytest.raises(ModelResponseInvalid, match="非有限"):
        run(gateway.embed_texts(["一"], tenant_id=1))


def test_zero_vector_rejected():
    def responder(**kwargs):
        response = ok_responder(DIM)(**kwargs)
        response.data[0].embedding = [0.0] * DIM
        return response

    gateway = make_gateway(responder)
    with pytest.raises(ModelResponseInvalid, match="范数"):
        run(gateway.embed_texts(["一"], tenant_id=1))


def test_vectors_l2_normalized():
    gateway = make_gateway(ok_responder(DIM))
    result = run(gateway.embed_texts(["甲", "乙"], tenant_id=1))
    norms = np.linalg.norm(result.vectors, axis=1)
    assert np.allclose(norms, 1.0, atol=1e-5)


def test_invalid_response_not_retried():
    def responder(**kwargs):
        response = ok_responder(DIM)(**kwargs)
        response.data.pop(0)
        return response

    gateway = make_gateway(responder)
    with pytest.raises(ModelResponseInvalid):
        run(gateway.embed_texts(["一", "二"], tenant_id=1))
    assert len(gateway._client.embeddings.calls) == 1  # 协议错误不重试


def test_rate_limit_then_success():
    failures = [FakeAPIError(429, {"retry-after": "0"}), FakeAPIError(429, {"retry-after": "0"})]
    gateway = make_gateway(FlakyResponder(failures))
    result = run(gateway.embed_texts(["一"], tenant_id=1))
    assert result.vectors.shape == (1, DIM)
    assert len(gateway._client.embeddings.calls) == 3


def test_cache_hit_within_tenant():
    gateway = make_gateway(ok_responder(DIM))
    texts = ["甲", "乙"]
    first = run(gateway.embed_texts(texts, tenant_id=1))
    second = run(gateway.embed_texts(texts, tenant_id=1))
    assert first.cache_hits == 0
    assert second.cache_hits == 2
    assert len(gateway._client.embeddings.calls) == 1  # 第二次未再请求
    assert np.allclose(first.vectors, second.vectors)


def test_cache_tenant_scoped():
    gateway = make_gateway(ok_responder(DIM))
    run(gateway.embed_texts(["共享文本"], tenant_id=1))
    run(gateway.embed_texts(["共享文本"], tenant_id=2))
    assert len(gateway._client.embeddings.calls) == 2  # 跨租户不复用


def test_duplicates_within_request_deduped():
    gateway = make_gateway(ok_responder(DIM))
    result = run(gateway.embed_texts(["相同文本", "相同文本", "其他"], tenant_id=1))
    assert [len(call["input"]) for call in gateway._client.embeddings.calls] == [2]
    assert result.vectors.shape == (3, DIM)
    assert np.allclose(result.vectors[0], result.vectors[1])


def test_input_too_long_rejected():
    settings = Settings(embedding_input_max_bytes=8)
    gateway = make_gateway(ok_responder(DIM), settings=settings)
    with pytest.raises(ModelInputTooLong):
        run(gateway.embed_texts(["员工报销制度" * 10], tenant_id=1))


def test_empty_inputs():
    gateway = make_gateway(ok_responder(DIM))
    empty = run(gateway.embed_texts([], tenant_id=1))
    assert empty.vectors.shape == (0, DIM)
    with pytest.raises(ValueError):
        run(gateway.embed_texts([""], tenant_id=1))


def test_budget_exceeded_before_call():
    budget = BudgetTracker(Settings(model_per_file_token_budget=1))
    gateway = make_gateway(ok_responder(DIM), budget=budget)
    with pytest.raises(ModelBudgetExceeded):
        run(gateway.embed_texts(["一段不小的文本内容"], tenant_id=1, budget_scope_id="file-9"))
    assert len(gateway._client.embeddings.calls) == 0
