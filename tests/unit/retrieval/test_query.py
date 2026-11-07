"""查询预处理单测：分词口径、同义词扩展、指代消解（实现文档 §7.1）。"""

from app.retrieval.query import preprocess


def test_preprocess_terms_and_entities():
    plan = preprocess("RAG 系统 v2.1 的 API 限额是多少？")
    assert "限额" in plan.terms
    assert ("version", "v2.1") in plan.entities
    assert plan.original == "RAG 系统 v2.1 的 API 限额是多少？"


def test_synonym_expansion_bounded():
    plan = preprocess("报销期限是多久？")
    assert "期限" in plan.terms and "时限" in plan.terms  # 组内补全
    plan2 = preprocess("考勤记录在哪里查看？")
    assert "时限" not in plan2.terms  # 无关词不扩展
    assert any("考勤" in term for term in plan2.terms)


def test_pronoun_with_unique_entity_expands():
    history = [{"role": "assistant", "content": "v2.1.3 已于上周发布。"}]
    plan = preprocess("它的限额是多少？", history)
    assert plan.clarification_required is False
    assert "v2.1.3" in plan.normalized  # 实体已并入检索问题
    assert ("coref", "v2.1.3") in plan.entities


def test_pronoun_with_multiple_entities_requires_clarification():
    history = [{"role": "assistant", "content": "v2.1.3 限额 500元，生效日期 2026年11月7日。"}]
    plan = preprocess("它怎么规定的？", history)
    assert plan.clarification_required is True


def test_pronoun_without_history_requires_clarification():
    plan = preprocess("它的限额是多少？")
    assert plan.clarification_required is True


def test_no_pronoun_ignores_history():
    history = [{"role": "assistant", "content": "v2.1.3 已发布。"}]
    plan = preprocess("报销期限是多久？", history)
    assert plan.clarification_required is False
    assert "v2.1.3" not in plan.normalized  # 历史不作证据，不并入


def test_history_content_never_becomes_terms():
    plan = preprocess("考勤规定是什么？", history=[{"role": "user", "content": "报销时限 30 日"}])
    assert "考勤" in plan.terms
    assert "时限" not in plan.terms or "报销" not in plan.terms  # 历史词项不混入
