"""问答模型网关：qwen3.8-flash，关闭思考模式，校验 finish_reason（实现文档 §6.2/§8.3/§10.3）。

生成失败抛 GenerationUnavailable，调用方保留已检索来源，不把错误伪装成答案；
finish_reason 非 stop 属于业务层可重生成一次的范畴（M6 校验逻辑），网关只如实报告。
"""

from __future__ import annotations

from dataclasses import dataclass

from openai import AsyncOpenAI

from app.core.config import Settings, get_settings
from app.gateways.budget import GLOBAL_SCOPE, BudgetTracker
from app.gateways.embedding import TokenUsage
from app.gateways.errors import (
    GenerationIncomplete,
    GenerationUnavailable,
    ModelAuthError,
    ModelResponseInvalid,
)
from app.gateways.retry import with_retries


@dataclass
class ChatResult:
    content: str
    finish_reason: str
    usage: TokenUsage


class ChatGateway:
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

    @classmethod
    def from_settings(
        cls, settings: Settings | None = None, *, budget: BudgetTracker | None = None
    ) -> ChatGateway:
        settings = settings or get_settings()
        if not settings.model_configured:
            raise ModelAuthError("MODEL_BASE_URL / DASHSCOPE_API_KEY 未配置")
        client = AsyncOpenAI(
            api_key=settings.dashscope_api_key,
            base_url=settings.model_base_url,
            timeout=settings.model_timeout_seconds,
            max_retries=0,
        )
        return cls(client, settings=settings, budget=budget)

    async def generate(
        self,
        *,
        system_prompt: str,
        user_payload: str,
        max_tokens: int | None = None,
        budget_scope_id: str = GLOBAL_SCOPE,
    ) -> ChatResult:
        """生成候选答案；调用成功不等于答案事实正确，校验由 generation 模块负责。"""
        if self._budget:
            estimate = (len(system_prompt) + len(user_payload)) // 3 + 1
            self._budget.ensure_within(estimated_tokens=estimate, scope_id=budget_scope_id)

        def call():
            return self._client.chat.completions.create(
                model=self._settings.chat_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_payload},
                ],
                max_tokens=max_tokens or self._settings.chat_max_output_tokens,
                extra_body={"enable_thinking": False},  # 实现文档 §8.3
            )

        response = await with_retries(call, settings=self._settings)
        choices = getattr(response, "choices", None)
        if not choices:
            raise ModelResponseInvalid("chat 响应缺少 choices")
        choice = choices[0]
        finish_reason = getattr(choice, "finish_reason", None)
        if finish_reason != "stop":
            raise GenerationIncomplete(f"finish_reason={finish_reason}")
        content = (choice.message.content or "").strip()
        if not content:
            raise GenerationIncomplete("生成内容为空")
        usage = TokenUsage(
            prompt_tokens=getattr(response.usage, "prompt_tokens", 0) or 0,
            total_tokens=getattr(response.usage, "total_tokens", 0) or 0,
        )
        if self._budget:
            self._budget.add(tokens=usage.total_tokens, scope_id=budget_scope_id)
        return ChatResult(content, finish_reason, usage)

    async def generate_stream(
        self,
        *,
        system_prompt: str,
        user_payload: str,
        max_tokens: int | None = None,
        budget_scope_id: str = GLOBAL_SCOPE,
    ):
        """流式生成：异步产出 {"delta": str, "finish_reason": str|None}。

        - 重试只覆盖建连阶段（首块之前）；流开始后错误转为 GenerationUnavailable；
        - 结束时校验 finish_reason；usage 优先经 stream_options 获取，
          兼容端点不支持时退回无 stream_options 并按字符数记账。
        """
        if self._budget:
            estimate = (len(system_prompt) + len(user_payload)) // 3 + 1
            self._budget.ensure_within(estimated_tokens=estimate, scope_id=budget_scope_id)

        async def _open(stream_options: dict | None):
            return await self._client.chat.completions.create(
                model=self._settings.chat_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_payload},
                ],
                max_tokens=max_tokens or self._settings.chat_max_output_tokens,
                extra_body={"enable_thinking": False},  # 实现文档 §8.3
                stream=True,
                **({"stream_options": stream_options} if stream_options else {}),
            )

        try:
            stream = await with_retries(
                lambda: _open({"include_usage": True}), settings=self._settings
            )
        except Exception as exc:  # noqa: BLE001  部分兼容端点不支持 stream_options
            if "stream_options" in str(exc):
                stream = await with_retries(lambda: _open(None), settings=self._settings)
            else:
                raise

        total = 0
        finish_reason = None
        try:
            async for chunk in stream:
                choices = getattr(chunk, "choices", None)
                if not choices:
                    usage_chunk = getattr(chunk, "usage", None)
                    if usage_chunk is not None:
                        total = getattr(usage_chunk, "total_tokens", 0) or total
                    continue
                delta = choices[0].delta
                piece = (getattr(delta, "content", None) or "") if delta else ""
                finish_reason = getattr(choices[0], "finish_reason", None) or finish_reason
                if piece:
                    total += len(piece)
                    yield {"delta": piece, "finish_reason": None}
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, (GenerationIncomplete, ModelResponseInvalid)):
                raise
            raise GenerationUnavailable(f"流式生成中断：{exc.__class__.__name__}") from exc

        if finish_reason != "stop":
            raise GenerationIncomplete(f"finish_reason={finish_reason}")
        if self._budget:
            self._budget.add(tokens=total, scope_id=budget_scope_id)
