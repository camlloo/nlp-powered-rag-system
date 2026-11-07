"""规则切块单测：§4.5 示例、长度约束、重叠、跨小节隔离、表格与长句兜底。"""

from app.core.config import Settings
from app.nlp.chunker import chunk_document
from app.parsers.markdown import parse_markdown
from app.schemas.contracts import ParsedDocument, TextBlock


def _md(text: str) -> ParsedDocument:
    return parse_markdown(text.encode(), "doc", Settings())


def test_reimburse_example_from_doc():
    """实现文档 §4.5 示例：标题路径 + 两条原始规则 + 原文位置，不改写不摘要。"""
    parsed = _md("# 第三章 费用报销\n\n员工应在出差结束后 30 日内申请。交通费凭有效票据报销。\n")
    pieces = chunk_document(parsed, Settings())
    assert len(pieces) == 1
    piece = pieces[0]
    assert piece.title_path == ["第三章 费用报销"]
    assert "员工应在出差结束后 30 日内申请。" in piece.content
    assert "交通费凭有效票据报销。" in piece.content
    # 不得改写为损失期限信息的摘要
    assert "及时报销" not in piece.content
    assert piece.locators


def test_chunk_size_limits_and_overlap():
    sentence = "第{i}条：员工应当在规定期限内提交完整的报销材料并等待审核结果。"
    text = "# 制度\n\n" + "".join(sentence.format(i=i) for i in range(60)) + "\n"
    pieces = chunk_document(_md(text), Settings())
    assert len(pieces) >= 2
    for piece in pieces:
        assert len(piece.content) <= 1000  # 硬上限
    # 相邻块存在完整句重叠：后块开头片段出现在前块尾部
    for prev, nxt in zip(pieces, pieces[1:], strict=False):
        head = nxt.content[:20]
        assert head in prev.content or len(prev.content) < 300


def test_no_cross_section_merge():
    text = (
        "# A 章节\n\n" + "甲方内容。" * 50 + "\n\n"
        "# B 章节\n\n" + "乙方内容。" * 50 + "\n"
    )
    pieces = chunk_document(_md(text), Settings())
    paths = {tuple(p.title_path) for p in pieces}
    assert paths == {("A 章节",), ("B 章节",)}
    for piece in pieces:
        if piece.title_path == ["A 章节"]:
            assert "乙方内容" not in piece.content
        else:
            assert "甲方内容" not in piece.content


def test_long_sentence_hard_split():
    text = "# 长句\n\n" + "该条款涉及大量连续描述没有任何标点" * 120 + "。后续短句。\n"
    pieces = chunk_document(_md(text), Settings())
    assert pieces
    for piece in pieces:
        assert len(piece.content) <= 1000


def test_tiny_fragments_merged_in_section():
    text = "# 小节\n\n短句一。\n\n短句二。\n\n短句三。\n"
    pieces = chunk_document(_md(text), Settings())
    assert len(pieces) == 1
    assert "短句一。" in pieces[0].content and "短句三。" in pieces[0].content


def test_table_chunking_repeats_header():
    doc = ParsedDocument(
        fmt="docx",
        title="表测",
        blocks=[
            TextBlock(block_no=1, kind="heading", text="附表", level=1,
                      locator={"type": "docx", "paragraph": 1}),
            *[
                TextBlock(
                    block_no=2 + i,
                    kind="table_row",
                    text="行",
                    locator={"type": "docx", "paragraph": 2 + i, "row": i + 1},
                    table={
                        "table_no": 1,
                        "row_no": i + 1,
                        "headers": ["项目", "说明"],
                        "row": [f"第{i}行", "数据" * 20],
                    },
                )
                for i in range(30)
            ],
        ],
    )
    pieces = chunk_document(doc, Settings())
    assert len(pieces) >= 2
    for piece in pieces:
        assert piece.is_table
        assert piece.content.startswith("表格1 表头：项目 | 说明")  # 长表重复表头
        assert len(piece.content) <= 700
