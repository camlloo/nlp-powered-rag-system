"""内部数据契约：解析输出与切块契约（实现文档 §4.6）。

契约由 parsers、nlp、indexing 共用；字段语义以实现文档为准：
- content 展示正文保留数值、单位、否定与例外；
- search_text 规范化检索文本，与 query 使用相同词项处理版本；
- embedding_input 标题前缀与正文，独立计算哈希；
- source_locator 为原文位置 JSON（PDF 页码或 DOCX 段落与表格位置）。
"""

from typing import Any, Literal

from pydantic import BaseModel, Field

BlockKind = Literal["paragraph", "heading", "table_row", "code"]
DocFormat = Literal["txt", "markdown", "docx", "pdf"]


class TableInfo(BaseModel):
    table_no: int
    row_no: int
    headers: list[str] = Field(default_factory=list)
    row: list[str] = Field(default_factory=list)


class TextBlock(BaseModel):
    """解析器输出的最小文本单元，带原文定位。"""

    block_no: int
    kind: BlockKind
    text: str
    level: int | None = None  # 标题级别 1-6
    locator: dict[str, Any] = Field(default_factory=dict)
    table: TableInfo | None = None


class QualityReport(BaseModel):
    """文本型 PDF 质量打分（实现文档 §4.1），低于阈值转 needs_review。"""

    readable_ratio: float
    abnormal_char_ratio: float
    min_page_chars: int
    needs_review: bool = False
    reasons: list[str] = Field(default_factory=list)


class ParsedDocument(BaseModel):
    fmt: DocFormat
    title: str  # 文件名去扩展名或首个一级标题
    blocks: list[TextBlock] = Field(default_factory=list)
    encoding: str | None = None  # txt 最终采用的编码
    page_count: int | None = None
    quality: QualityReport | None = None


class ChunkDraft(BaseModel):
    """§4.6 处理结果契约（入库前形态，chunk_no 与外键由 indexing 分配）。"""

    content: str
    search_text: str
    embedding_input: str
    tokens: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    title_path: list[str] = Field(default_factory=list)
    source_locator: dict[str, Any] = Field(default_factory=dict)


class PipelineResult(BaseModel):
    pipeline_version: str
    chunks: list[ChunkDraft] = Field(default_factory=list)
    duplicates_removed: int = 0
    near_duplicate_pairs: list[tuple[int, int]] = Field(default_factory=list)
    needs_review: bool = False
    review_reasons: list[str] = Field(default_factory=list)
