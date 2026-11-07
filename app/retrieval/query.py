"""查询预处理：归一化、同词典分词、实体抽取、受控同义词扩展与指代消解（实现文档 §7.1）。

- 原始 query 保留供展示；向量输入使用轻量规范化文本（§6.1），
  词项通道使用停用词过滤后的 tokens（与文档同口径）；
- 同义词表受控且内置（词元粒度），只做有限扩展，不合并不同业务概念；
- 首期仅支持规则化追问补全：出现明确指代且上一轮有唯一实体时，把该实体
  加入检索问题；有歧义返回 clarification_required；
- 历史对话只用于指代消解，绝不作为新的事实证据（检索永远面向当前快照）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.nlp.cleaning import normalize_search_text
from app.nlp.entities import extract_entities
from app.nlp.tokenizer import content_terms

# 受控同义词组（词元粒度；组内任一词出现则补全其余词）
SYNONYM_GROUPS: tuple[frozenset[str], ...] = (
    frozenset({"时限", "期限"}),
    frozenset({"账号", "账户"}),
    frozenset({"额度", "限额"}),
)

# 明确指代词：命中才尝试追问补全
COREFERENCE_MARKERS = ("它", "其", "该产品", "该制度", "该文件", "上述规定", "此产品")


@dataclass
class QueryPlan:
    original: str
    normalized: str  # 轻量规范化文本（向量输入口径）
    terms: list[str]  # 词项通道输入（含同义词扩展）
    entities: list[tuple[str, str]] = field(default_factory=list)  # (type, text)
    clarification_required: bool = False


def preprocess(question: str, history: list[dict] | None = None) -> QueryPlan:
    question = question.strip()
    normalized = normalize_search_text(question)
    terms = content_terms(normalized)
    entities = [(e.type, e.text) for e in extract_entities(question)]
    terms = _expand_synonyms(terms)
    entities.extend(_dictionary_entities(normalized))

    resolved_entity = _resolve_coreference(question, history)
    if resolved_entity == "__ambiguous__":
        return QueryPlan(
            original=question,
            normalized=normalized,
            terms=terms,
            entities=entities,
            clarification_required=True,
        )
    if resolved_entity:
        normalized = normalize_search_text(f"{normalized} {resolved_entity}")
        terms = _expand_synonyms(content_terms(normalized))
        entities.append(("coref", resolved_entity))
    return QueryPlan(
        original=question, normalized=normalized, terms=terms, entities=entities
    )


def _expand_synonyms(terms: list[str]) -> list[str]:
    term_set = set(terms)
    for group in SYNONYM_GROUPS:
        if term_set & group:
            term_set |= group
    # 保持原顺序在前、扩展词在后，确定性输出
    seen: set[str] = set()
    ordered: list[str] = []
    for term in terms + sorted(term_set - set(terms)):
        if term not in seen:
            seen.add(term)
            ordered.append(term)
    return ordered


def _dictionary_entities(text: str) -> list[tuple[str, str]]:
    from app.nlp.entities import dictionary_entities

    return [(e.type, e.text) for e in dictionary_entities(text)]


def _resolve_coreference(question: str, history: list[dict] | None) -> str | None:
    """返回需追加的实体文本；"__ambiguous__" 表示需澄清；None 表示无需处理。"""
    if not any(marker in question for marker in COREFERENCE_MARKERS):
        return None
    if not history:
        return "__ambiguous__"
    last = history[-1].get("content", "")
    unique: dict[str, None] = {}
    for entity in extract_entities(last):
        unique.setdefault(entity.text, None)
    for _, text in _dictionary_entities(last):
        unique.setdefault(text, None)
    if len(unique) == 1:
        return next(iter(unique))
    return "__ambiguous__"  # 零个或多个实体：歧义，需澄清
