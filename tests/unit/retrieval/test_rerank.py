"""规则重排单测：标题加权、实体加权、版本过滤（实现文档 §7.4）。"""

from dataclasses import dataclass

import pytest

from app.retrieval.rerank import rerank


@dataclass
class FakeRecord:
    title_path: str
    content: str


def test_title_bonus_promotes_match():
    records = {
        1: FakeRecord(title_path="考勤规定", content="迟到按旷工处理"),
        2: FakeRecord(title_path="报销流程", content="迟到按旷工处理"),
    }
    fused = [(1, 0.02), (2, 0.02)]  # 融合同分
    result = rerank(["考勤"], [], fused, records, top_k=10)
    assert result[0].chunk_id == 1  # 标题命中"考勤"的块靠前
    assert result[0].title_bonus > 0
    assert result[1].title_bonus == 0
    assert result[0].rerank_score == pytest.approx(0.02 + 0.05)


def test_entity_bonus():
    records = {1: FakeRecord(title_path="", content="网关 v2.1.3 限额 500元")}
    fused = [(1, 0.0)]
    result = rerank(["限额"], [("version", "v2.1.3"), ("money", "500元")], fused, records)
    # 实体封顶 2 处：两个实体都命中 → +0.10
    assert result[0].entity_bonus == pytest.approx(0.10)


def test_version_filter_drops_other_version_only():
    records = {
        1: FakeRecord(title_path="", content="网关 v2.0.1 限额 300元"),  # 仅其他版本 → 过滤
        2: FakeRecord(title_path="", content="网关 v2.1.3 限额 500元"),  # 所问版本 → 保留
        3: FakeRecord(title_path="", content="通用说明，未提版本"),  # 无版本 → 保留
    }
    fused = [(1, 0.03), (2, 0.02), (3, 0.01)]
    result = rerank(["限额"], [("version", "v2.1.3")], fused, records)
    ids = [item.chunk_id for item in result]
    assert 1 not in ids
    assert set(ids) == {2, 3}


def test_opposite_condition_paragraphs_kept():
    # 重排不做任何近重复删除：含相反条件的两个块都保留
    records = {
        1: FakeRecord(title_path="", content="试用期员工不适用本条款"),
        2: FakeRecord(title_path="", content="试用期员工适用本条款"),
    }
    fused = [(1, 0.01), (2, 0.01)]
    result = rerank([], [], fused, records)
    assert {item.chunk_id for item in result} == {1, 2}


def test_top_k_cap():
    records = {i: FakeRecord(title_path="", content="内容") for i in range(50)}
    fused = [(i, 0.01) for i in range(50)]
    result = rerank([], [], fused, records, top_k=30)
    assert len(result) == 30
