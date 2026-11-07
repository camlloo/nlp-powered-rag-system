"""预算追踪单测：日预算、范围预算、不限额。"""

import pytest

from app.core.config import Settings
from app.gateways.budget import BudgetTracker
from app.gateways.errors import ModelBudgetExceeded


def test_daily_budget_enforced():
    tracker = BudgetTracker(Settings(model_daily_token_budget=100))
    tracker.add(tokens=90)
    with pytest.raises(ModelBudgetExceeded, match="日预算"):
        tracker.ensure_within(estimated_tokens=11)
    tracker.ensure_within(estimated_tokens=10)  # 恰好不超


def test_scope_budget_enforced():
    tracker = BudgetTracker(Settings(model_per_file_token_budget=50))
    tracker.add(tokens=40, scope_id="file-1")
    with pytest.raises(ModelBudgetExceeded, match="file-1"):
        tracker.ensure_within(estimated_tokens=11, scope_id="file-1")
    # 其他范围不受影响
    tracker.ensure_within(estimated_tokens=11, scope_id="file-2")


def test_zero_budget_means_unlimited():
    tracker = BudgetTracker(Settings(model_daily_token_budget=0, model_per_file_token_budget=0))
    tracker.add(tokens=10**9)
    tracker.ensure_within(estimated_tokens=10**9)


def test_usage_accounting():
    tracker = BudgetTracker(Settings())
    tracker.add(tokens=10, scope_id="file-1")
    tracker.add(tokens=5, scope_id="file-1")
    tracker.add(tokens=100)  # 仅计入全局
    assert tracker.usage() == 115
    assert tracker.usage("file-1") == 15
