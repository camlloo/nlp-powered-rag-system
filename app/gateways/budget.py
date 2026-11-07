"""模型调用预算：日预算与按范围（如单文件）预算计数（实现文档 §6.2）。

进程内计数即可满足首期（单 API + 单 worker）；按 UTC 日滚动。
预算为 0 表示不限制。
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict

from app.core.config import Settings
from app.gateways.errors import ModelBudgetExceeded

GLOBAL_SCOPE = "global"


def _today_key() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y%m%d")


class BudgetTracker:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._tokens: dict[tuple[str, str], int] = defaultdict(int)

    def ensure_within(self, *, estimated_tokens: int, scope_id: str = GLOBAL_SCOPE) -> None:
        """调用前检查；超预算抛 ModelBudgetExceeded（不重试）。"""
        today = _today_key()
        daily_limit = self._settings.model_daily_token_budget
        if daily_limit and self._tokens[(today, GLOBAL_SCOPE)] + estimated_tokens > daily_limit:
            raise ModelBudgetExceeded(
                f"日预算 {daily_limit} 不足（已用 {self._tokens[(today, GLOBAL_SCOPE)]}）"
            )
        if scope_id != GLOBAL_SCOPE:
            per_scope = self._settings.model_per_file_token_budget
            if per_scope and self._tokens[(today, scope_id)] + estimated_tokens > per_scope:
                raise ModelBudgetExceeded(
                    f"范围 {scope_id} 预算 {per_scope} 不足"
                    f"（已用 {self._tokens[(today, scope_id)]}）"
                )

    def add(self, *, tokens: int, scope_id: str = GLOBAL_SCOPE) -> None:
        today = _today_key()
        self._tokens[(today, GLOBAL_SCOPE)] += tokens
        if scope_id != GLOBAL_SCOPE:
            self._tokens[(today, scope_id)] += tokens

    def usage(self, scope_id: str = GLOBAL_SCOPE) -> int:
        return self._tokens[(_today_key(), scope_id)]
