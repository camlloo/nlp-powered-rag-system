"""答案校验单测（实现文档 §8.3：引用编号存在性与完整性）。"""

from app.generation.validator import validate_answer


def test_valid_answer():
    assert validate_answer("员工应在 30 日内申请。[S1][S2]", {"S1", "S2"}).ok is True


def test_missing_citation_rejected():
    result = validate_answer("没有任何引用的答案。", {"S1"})
    assert result.ok is False and result.error_code == "missing_citation"


def test_fabricated_citation_rejected():
    result = validate_answer("编造的引用 [S9]。", {"S1"})
    assert result.ok is False
    assert result.error_code == "invalid_citation"
    assert result.invalid_sources == ["S9"]


def test_mixed_citations_rejected_with_invalid_list():
    result = validate_answer("部分有效 [S1] 部分编造 [S3] [S2]。", {"S1", "S2"})
    assert result.ok is False
    assert result.invalid_sources == ["S3"]


def test_empty_answer_rejected():
    result = validate_answer("  ", {"S1"})
    assert result.ok is False and result.error_code == "empty_answer"
    assert validate_answer(None, {"S1"}).ok is False
