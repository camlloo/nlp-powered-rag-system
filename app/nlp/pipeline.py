"""传统 NLP 流水线编排：解析结果 -> 清洗 -> 切块 -> 词项/关键词 -> 契约输出（实现文档 §4.6）。

全程不调用任何模型；pipeline_version 指纹包含解析、清洗、切块与词典版本，
规则或资源变更时指纹变化，触发下游缓存与索引幂等重建。
"""

from __future__ import annotations

import hashlib

from app.core.config import Settings, get_settings
from app.nlp import chunker, cleaning, dedup, keywords, tokenizer
from app.schemas.contracts import ChunkDraft, ParsedDocument, PipelineResult

# 规则版本：清洗/分句/分词/切块任一规则语义变更时递增
RULES_VERSION = "1"


def pipeline_version(settings: Settings) -> str:
    digest = hashlib.sha256()
    digest.update(RULES_VERSION.encode())
    digest.update(tokenizer.resource_fingerprint().encode())
    digest.update(
        f"{settings.chunk_max_chars}-{settings.chunk_overlap_chars}-"
        f"{settings.chunk_hard_max_chars}-{settings.chunk_min_chars}".encode()
    )
    return f"p{RULES_VERSION}_{digest.hexdigest()[:12]}"


def build_embedding_input(
    doc_title: str, title_path: list[str], content: str, settings: Settings
) -> str:
    """embedding_input 采用"文档标题、标题路径、正文"稳定模板，前缀限长（§4.5/§6.1）。

    文档标题与标题路径连续重复的部分只保留一份（如 Markdown 首个 H1 即文档标题）。
    """
    parts: list[str] = []
    for item in [doc_title, *title_path]:
        item = item.strip()
        if item and (not parts or parts[-1] != item):
            parts.append(item)
    prefix = " > ".join(parts)
    limit = settings.embedding_title_prefix_chars
    if len(prefix) > limit:
        prefix = prefix[:limit]
    return f"{prefix}\n{content}" if prefix else content


def run_pipeline(parsed: ParsedDocument, settings: Settings | None = None) -> PipelineResult:
    settings = settings or get_settings()

    if parsed.quality and parsed.quality.needs_review:
        return PipelineResult(
            pipeline_version=pipeline_version(settings),
            chunks=[],
            needs_review=True,
            review_reasons=list(parsed.quality.reasons),
        )

    if parsed.fmt == "pdf":
        cleaning.remove_repeated_page_lines(parsed)  # 原位剔除页眉页脚

    pieces = chunker.chunk_document(parsed, settings)

    drafts: list[ChunkDraft] = []
    for piece in pieces:
        search_text = cleaning.normalize_search_text(cleaning.clean_display_text(piece.content))
        drafts.append(
            ChunkDraft(
                content=piece.content,
                search_text=search_text,
                embedding_input="",
                tokens=tokenizer.content_terms(search_text),
                keywords=[],
                title_path=piece.title_path,
                source_locator=_locator(parsed.fmt, piece.locators),
            )
        )

    keyword_lists = keywords.extract_keywords([d.tokens for d in drafts])
    for draft, kws in zip(drafts, keyword_lists, strict=False):
        draft.keywords = kws

    # 块级去重：完全重复直接剔除；近重复仅生成候选供评测参考
    seen: set[bytes] = set()
    unique: list[ChunkDraft] = []
    duplicates_removed = 0
    for draft in drafts:
        digest = dedup.text_sha256(draft.search_text)
        if digest in seen:
            duplicates_removed += 1
            continue
        seen.add(digest)
        unique.append(draft)

    for draft in unique:
        draft.embedding_input = build_embedding_input(
            parsed.title, draft.title_path, draft.content, settings
        )

    near_pairs = dedup.near_duplicate_pairs(
        [(i, d.search_text) for i, d in enumerate(unique)]
    )

    return PipelineResult(
        pipeline_version=pipeline_version(settings),
        chunks=unique,
        duplicates_removed=duplicates_removed,
        near_duplicate_pairs=near_pairs,
        needs_review=False,
        review_reasons=[],
    )


def _locator(fmt: str, block_locators: list[dict]) -> dict:
    """块定位收敛为可引用的原文位置：保留类型与首末定位（引用用块级定位）。"""
    spans = [block_locators[0]] if block_locators else []
    if len(block_locators) > 1:
        spans.append(block_locators[-1])
    return {"type": fmt, "spans": spans}
