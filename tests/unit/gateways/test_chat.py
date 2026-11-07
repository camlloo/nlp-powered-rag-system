"""问答网关单测：协议参数、finish_reason 校验、用量与重试。"""

import pytest

from app.core.config import Settings
from app.gateways.chat import ChatGateway
from app.gateways.errors import GenerationIncomplete, ModelResponseInvalid
from tests.unit.gateways.gateway_helpers import (
    FakeAPIError,
    FakeChatClient,
    FlakyResponder,
    chat_response,
    run,
)


def make_gateway(responder, settings=None) -> ChatGateway:
    return ChatGateway(FakeChatClient(responder), settings=settings or Settings())


def test_generate_success_and_protocol():
    gateway = make_gateway(lambda **kwargs: chat_response())
    result = run(
        gateway.generate(system_prompt="你是知识库问答助手", user_payload='{"question":"..."}')
    )
    assert result.content == "根据资料 [S1]，员工应在 30 日内申请。[S1]"
    assert result.finish_reason == "stop"
    assert result.usage.total_tokens == 80
    call = gateway._client.chat.completions.calls[0]
    assert call["model"] == "qwen3.8-flash"
    assert call["extra_body"] == {"enable_thinking": False}  # 实现文档 §8.3
    assert call["max_tokens"] == 1200
    assert call["messages"][0]["role"] == "system"


def test_max_tokens_override():
    gateway = make_gateway(lambda **kwargs: chat_response())
    run(gateway.generate(system_prompt="s", user_payload="u", max_tokens=300))
    assert gateway._client.chat.completions.calls[0]["max_tokens"] == 300


def test_finish_reason_length_rejected():
    gateway = make_gateway(lambda **kwargs: chat_response(finish_reason="length"))
    with pytest.raises(GenerationIncomplete, match="length"):
        run(gateway.generate(system_prompt="s", user_payload="u"))


def test_empty_content_rejected():
    gateway = make_gateway(lambda **kwargs: chat_response(content="  "))
    with pytest.raises(GenerationIncomplete, match="为空"):
        run(gateway.generate(system_prompt="s", user_payload="u"))


def test_missing_choices_rejected():
    def responder(**kwargs):
        response = chat_response()
        response.choices = []
        return response

    gateway = make_gateway(responder)
    with pytest.raises(ModelResponseInvalid, match="choices"):
        run(gateway.generate(system_prompt="s", user_payload="u"))


def test_rate_limit_then_success():
    gateway = make_gateway(
        FlakyResponder([FakeAPIError(429, {"retry-after": "0"}), FakeAPIError(500)])
    )

    class FlakyChat(FlakyResponder):
        pass

    # FlakyResponder 签名与 chat responder 不同，这里手动实现状态翻转
    state = {"n": 0}

    def responder(**kwargs):
        state["n"] += 1
        if state["n"] == 1:
            raise FakeAPIError(429, {"retry-after": "0"})
        if state["n"] == 2:
            raise FakeAPIError(500)
        return chat_response()

    gateway = make_gateway(responder)
    result = run(gateway.generate(system_prompt="s", user_payload="u"))
    assert "30 日内" in result.content
    assert state["n"] == 3
