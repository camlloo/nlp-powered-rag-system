"""流水线单测：契约七字段、指纹稳定、去重、needs_review 短路（实现文档 §4.6）。"""

from pathlib import Path

from app.core.config import Settings
from app.nlp.pipeline import build_embedding_input, pipeline_version, run_pipeline
from app.parsers.intake import parse_upload
from app.parsers.markdown import parse_markdown
from app.schemas.contracts import ParsedDocument, QualityReport, TextBlock

FIXTURES = Path(__file__).parents[2] / "fixtures"


def _run_md():
    payload = (FIXTURES / "sample_doc.md").read_bytes()
    parsed = parse_upload("sample_doc.md", "text/markdown", payload, Settings())
    return run_pipeline(parsed, Settings())


def test_pipeline_contract_fields():
    result = _run_md()
    assert result.needs_review is False
    assert result.chunks
    for chunk in result.chunks:
        assert chunk.content
        assert chunk.search_text
        assert chunk.content in chunk.embedding_input  # 前缀 + 换行 + 正文
        assert isinstance(chunk.tokens, list)
        assert 0 < len(chunk.keywords) <= 8
        assert chunk.source_locator["type"] == "markdown"
        assert chunk.source_locator["spans"]


def test_pipeline_version_stable_and_sensitive():
    settings = Settings()
    v1 = pipeline_version(settings)
    assert pipeline_version(settings) == v1
    assert pipeline_version(Settings(chunk_max_chars=600)) != v1


def test_pipeline_embedding_prefix_uses_title_and_path():
    settings = Settings()
    text = "# 员工手册\n\n## 考勤规定\n\n员工应当按时签到。\n".encode()
    result = run_pipeline(parse_markdown(text, "t", settings), settings)
    assert result.chunks[0].embedding_input.split("\n")[0] == "员工手册 > 考勤规定"


def test_pipeline_embedding_input_prefix_dedup():
    """Markdown 首个 H1 即文档标题：前缀连续重复部分只保留一份。"""
    settings = Settings()
    text = "# 手册\n\n正文内容。".encode()
    result = run_pipeline(parse_markdown(text, "t", settings), settings)
    assert result.chunks[0].embedding_input.split("\n")[0] == "手册"


def test_pipeline_embedding_input_prefix_truncated():
    settings = Settings(embedding_title_prefix_chars=10)
    truncated = build_embedding_input("很长的文档标题" * 10, [], "正文", settings)
    assert truncated.split("\n")[0] == ("很长的文档标题" * 10)[:10]


def test_pipeline_dedups_identical_sections():
    locator = {"type": "docx", "paragraph": 1}
    parsed = ParsedDocument(
        fmt="docx",
        title="重复",
        blocks=[
            TextBlock(block_no=1, kind="heading", text="A 章节", level=1, locator=locator),
            TextBlock(block_no=2, kind="paragraph", text="完全相同的内容。", locator=locator),
            TextBlock(block_no=3, kind="heading", text="A 章节", level=1, locator=locator),
            TextBlock(block_no=4, kind="paragraph", text="完全相同的内容。", locator=locator),
        ],
    )
    result = run_pipeline(parsed, Settings())
    assert result.duplicates_removed == 1
    assert len(result.chunks) == 1


def test_pipeline_near_duplicate_pairs_reported():
    locator = {"type": "txt", "line": 1}
    text_a = "员工应当在出差结束后的三十日内提交报销申请" * 4
    text_b = text_a[:-1] + "地"  # 单字符差异，构成近重复候选
    parsed = ParsedDocument(
        fmt="txt",
        title="近重复",
        blocks=[
            TextBlock(block_no=1, kind="heading", text="S1", level=1, locator=locator),
            TextBlock(block_no=2, kind="paragraph", text=text_a, locator=locator),
            TextBlock(block_no=3, kind="heading", text="S2", level=1, locator=locator),
            TextBlock(
                block_no=4,
                kind="paragraph",
                text=text_b,
                locator=locator,
            ),
        ],
    )
    result = run_pipeline(parsed, Settings())
    assert len(result.chunks) == 2
    assert (0, 1) in result.near_duplicate_pairs


def test_pipeline_needs_review_short_circuit():
    parsed = ParsedDocument(
        fmt="pdf",
        title="扫描件",
        blocks=[],
        quality=QualityReport(
            readable_ratio=0.1,
            abnormal_char_ratio=0.9,
            min_page_chars=0,
            needs_review=True,
            reasons=["可读字符比例过低"],
        ),
    )
    result = run_pipeline(parsed, Settings())
    assert result.needs_review is True
    assert result.chunks == []
    assert result.review_reasons


def test_pipeline_negation_and_numbers_preserved():
    payload = (FIXTURES / "sample_zh_en.txt").read_bytes()
    parsed = parse_upload("sample_zh_en.txt", "text/plain", payload, Settings())
    result = run_pipeline(parsed, Settings())
    joined = "\n".join(c.content for c in result.chunks)
    for must_keep in ("30 日内", "禁止转售", "v2.1.3", "500元", "12.5%", "不可抗力"):
        assert must_keep in joined
    joined_search = "\n".join(c.search_text for c in result.chunks)
    assert "禁止转售" in joined_search  # 检索文本同样保留否定与例外
