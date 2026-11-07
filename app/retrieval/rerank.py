"""规则重排（实现文档 §7.4）。

对融合后的前 rerank_top_k 条执行：
- 标题路径命中查询词：每词 +TITLE_BONUS（封顶 3 词）；
- 显式实体（版本/型号编号/金额/百分比/日期）在正文精确出现：+ENTITY_BONUS（封顶 2 处）；
- 用户明确给定版本时，仅提及"其他版本"且不含所问版本的块被过滤；
- 含相反条件的段落保持独立——本模块不做任何近重复删除。

奖励为加性常数（初始值，须在评测集上调优），不包装为概率或置信度。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.nlp.entities import extract_entities

TITLE_BONUS = 0.05
TITLE_BONUS_CAP = 3
ENTITY_BONUS = 0.05
ENTITY_BONUS_CAP = 2

# 参与正文匹配奖励的实体类型
_MATCHABLE_TYPES = {"version", "id", "money", "percent", "date"}
_VERSION_TYPES = {"version"}


@dataclass
class RerankedItem:
    chunk_id: int
    fused_score: float
    rerank_score: float
    title_bonus: float
    entity_bonus: float


def rerank(
    query_terms: list[str],
    query_entities: list[tuple[str, str]],
    fused: list[tuple[int, float]],
    records: dict[int, object],  # chunk_id -> ChunkRecord
    *,
    top_k: int = 30,
) -> list[RerankedItem]:
    candidates = fused[: max(0, top_k)]
    query_versions = {
        text.lower() for etype, text in query_entities if etype in _VERSION_TYPES
    }
    results: list[RerankedItem] = []
    for chunk_id, fused_score in candidates:
        record = records.get(chunk_id)
        if record is None:
            continue
        content = record.content
        title_path = record.title_path

        matched_title = [t for t in set(query_terms) if t and t in title_path]
        title_bonus = min(len(matched_title), TITLE_BONUS_CAP) * TITLE_BONUS

        entity_bonus = 0.0
        matched_entities = 0
        for etype, text in query_entities:
            if etype in _MATCHABLE_TYPES and text.lower() in content.lower():
                matched_entities += 1
                if matched_entities <= ENTITY_BONUS_CAP:
                    entity_bonus += ENTITY_BONUS

        if query_versions and _mentions_only_other_versions(content, query_versions):
            continue  # 用户指定版本：过滤仅含其他版本的块（§7.4）

        results.append(
            RerankedItem(
                chunk_id=chunk_id,
                fused_score=fused_score,
                rerank_score=fused_score + title_bonus + entity_bonus,
                title_bonus=title_bonus,
                entity_bonus=entity_bonus,
            )
        )
    results.sort(key=lambda item: (-item.rerank_score, item.chunk_id))
    return results


def _mentions_only_other_versions(content: str, query_versions: set[str]) -> bool:
    chunk_versions = {e.text.lower() for e in extract_entities(content) if e.type == "version"}
    if not chunk_versions:
        return False  # 不含版本的块不过滤
    return not (chunk_versions & query_versions)
