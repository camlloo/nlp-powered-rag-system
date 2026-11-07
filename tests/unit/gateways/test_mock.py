"""Mock 网关单测：确定性向量、故障注入、调用记录。"""

import numpy as np
import pytest

from app.gateways.errors import EmbeddingUnavailable, GenerationUnavailable
from app.gateways.mock import MockChatGateway, MockEmbeddingGateway
from tests.unit.gateways.gateway_helpers import run


def test_mock_embedding_deterministic():
    gateway = MockEmbeddingGateway(dim=64)
    first = run(gateway.embed_texts(["甲文本", "乙文本"], tenant_id=1))
    second = run(gateway.embed_texts(["甲文本", "乙文本"], tenant_id=1))
    assert np.allclose(first.vectors, second.vectors)
    # 不同租户的向量不同（缓存与召回均按租户隔离）
    other = run(gateway.embed_texts(["甲文本"], tenant_id=2))
    assert not np.allclose(first.vectors[0], other.vectors[0])
    norms = np.linalg.norm(second.vectors, axis=1)
    assert np.allclose(norms, 1.0, atol=1e-5)


def test_mock_embedding_records_calls():
    gateway = MockEmbeddingGateway(dim=16)
    run(gateway.embed_texts(["a", "b"], tenant_id=1))
    assert gateway.calls == [["a", "b"]]


def test_mock_embedding_failure_injection():
    gateway = MockEmbeddingGateway(dim=16)
    gateway.failures.append(EmbeddingUnavailable("超时"))
    with pytest.raises(EmbeddingUnavailable, match="超时"):
        run(gateway.embed_texts(["a"], tenant_id=1))
    # 注入失败已弹出，之后恢复正常
    result = run(gateway.embed_texts(["a"], tenant_id=1))
    assert result.vectors.shape == (1, 16)


def test_mock_chat_reply_and_recording():
    gateway = MockChatGateway(reply="答案 [S1]")
    result = run(gateway.generate(system_prompt="sys", user_payload="user"))
    assert result.content == "答案 [S1]"
    assert result.finish_reason == "stop"
    assert gateway.calls[0]["system_prompt"] == "sys"
    assert gateway.calls[0]["max_tokens"] is None


def test_mock_chat_failure_injection():
    gateway = MockChatGateway()
    gateway.failures.append(GenerationUnavailable("限流"))
    with pytest.raises(GenerationUnavailable, match="限流"):
        run(gateway.generate(system_prompt="s", user_payload="u"))
