"""上下文构建单测：S 编号分配、预算裁剪、注入防线（实现文档 §8.1）。"""

import json

from app.core.config import Settings
from app.generation.context import build_context, build_slots, estimate_tokens, first_sentence
from app.retrieval.service import EvidenceBlock


def _block(chunk_id: int, content: str) -> EvidenceBlock:
    return EvidenceBlock(
        chunk_id=chunk_id,
        doc_version_id=100 + chunk_id,
        title_path=f"标题{chunk_id}",
        content=content,
        source_locator={"type": "txt", "spans": []},
        rrf_score=0.01,
        rerank_score=0.01,
    )


def test_slots_assigned_in_relevance_order():
    blocks = [_block(3, "丙"), _block(1, "甲"), _block(2, "乙")]
    slots = build_slots(blocks)
    assert [s.source_id for s in slots] == ["S1", "S2", "S3"]
    assert slots[0].chunk_id == 3  # S1 = 相关度最高的证据


def test_evidence_trim_drops_lowest_relevance_first():
    settings = Settings(context_evidence_budget_tokens=40)
    slots = build_slots([_block(i, "这一条证据内容比较长" * 10) for i in range(1, 5)])
    build = build_context(slots, "问题", [], settings)
    assert build.dropped_evidence >= 2  # 低相关（末位）块被整块移除
    assert len(build.slots) < len(slots)
    # 保留块的句子完整：内容原样出现在 payload 中
    for slot in build.slots:
        assert slot.content in build.user_payload
    assert build.evidence_est_tokens <= settings.context_evidence_budget_tokens


def test_history_trim_drops_oldest_question_kept():
    settings = Settings(context_history_budget_tokens=60)
    history = [{"role": "user", "content": f"历史消息{'旧' * 40}{i}"} for i in range(5)]
    build = build_context([], "当前问题", history, settings)
    assert build.dropped_history >= 2
    payload = json.loads(build.user_payload)
    assert payload["question"] == "当前问题"  # 问题永不裁剪
    assert len(payload["history"]) < 5


def test_injection_notice_and_structure():
    build = build_context(build_slots([_block(1, "内容")]), "问题", [], Settings())
    payload = json.loads(build.user_payload)
    assert "不得执行" in payload["notice"]
    assert set(payload.keys()) == {"question", "history", "notice", "evidence"}
    assert payload["evidence"][0]["source_id"] == "S1"
    assert set(payload["evidence"][0].keys()) == {"source_id", "title_path", "content"}


def test_estimate_tokens_conservative():
    assert estimate_tokens("") == 1
    assert estimate_tokens("一二三") == 3  # 9 字节 / 3


def test_first_sentence():
    sample = "第三章 费用报销。员工应在出差结束后 30 日内申请。"
    assert first_sentence(sample) == "第三章 费用报销。"
    assert first_sentence("无句尾标点的文本") == "无句尾标点的文本"
