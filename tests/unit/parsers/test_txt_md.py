"""TXT 编码回退与 Markdown 结构解析单测。"""

from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.errors import NeedsReview
from app.parsers.markdown import parse_markdown
from app.parsers.txt import parse_txt

FIXTURES = Path(__file__).parents[2] / "fixtures"


def test_txt_utf8():
    parsed = parse_txt("员工报销制度".encode(), "制度", Settings())
    assert parsed.encoding == "utf-8"
    assert parsed.blocks[0].text == "员工报销制度"
    assert parsed.blocks[0].locator == {"type": "txt", "line": 1}


def test_txt_fallback_encoding():
    # GB18030 字节流不是合法 UTF-8，回退成功并记录最终编码
    payload = "员工报销制度。".encode("gb18030")
    parsed = parse_txt(payload, "制度", Settings(fallback_encoding="gb18030"))
    assert parsed.encoding == "gb18030"
    assert "员工报销制度" in parsed.blocks[0].text


def test_txt_undecodable_needs_review():
    # 既非合法 UTF-8、也无法按回退编码解码
    payload = "员工报销".encode("gb18030")
    with pytest.raises(NeedsReview, match="解码"):
        parse_txt(payload, "制度", Settings(fallback_encoding="ascii"))


def test_markdown_structure():
    parsed = parse_markdown(
        (FIXTURES / "sample_doc.md").read_bytes(), "sample_doc", Settings()
    )
    assert parsed.title == "员工手册"  # 首个 H1
    kinds = [(b.kind, b.text) for b in parsed.blocks]
    assert kinds[0][0] == "heading" and kinds[0][1] == "员工手册"
    assert kinds[1] == ("heading", "考勤规定")
    assert any(kind == "code" and "reimburse" in text for kind, text in kinds)
    list_block = next(text for kind, text in kinds if kind == "paragraph" and "1. 出差" in text)
    assert "2. 附上有效票据与审批单。" in list_block  # 列表保留编号


def test_markdown_locator_line_numbers():
    parsed = parse_markdown(b"# Title\n\nbody line", "t", Settings())
    assert parsed.blocks[0].locator == {"type": "markdown", "line": 1}
    assert parsed.blocks[1].locator == {"type": "markdown", "line": 3}
