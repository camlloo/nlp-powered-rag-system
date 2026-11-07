"""规则实体抽取：日期、金额、百分比、编号、版本与业务词典实体（实现文档 §4.3）。

例如"RAG 系统 v2.1 的 API 限额"应保留 rag、v2.1、api 和限额。
供重排加权与近重复保护使用；不调用任何模型。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from app.nlp.tokenizer import load_stopwords

_ENTITY_RES: dict[str, list[re.Pattern[str]]] = {
    "date": [
        re.compile(r"\d{4}年\d{1,2}月\d{1,2}日"),
        re.compile(r"\d{4}-\d{1,2}-\d{1,2}"),
        re.compile(r"\d{1,2}月\d{1,2}日"),
        re.compile(r"\d+\s*(?:个?月|日|天|年|小时|分钟|周)"),
    ],
    "money": [
        re.compile(r"[¥￥]\s*\d[\d,，]*(?:\.\d+)?"),
        re.compile(r"\d[\d,，]*(?:\.\d+)?\s*(?:元|万元|人民币|美元)"),
    ],
    "percent": [re.compile(r"\d+(?:\.\d+)?\s*[%％]")],
    "version": [
        re.compile(r"[vV]\d+(?:\.\d+)+"),
        re.compile(r"\b\d+(?:\.\d+){2,}\b"),
    ],
    "id": [re.compile(r"\b[A-Za-z]{1,10}-\d+(?:[A-Za-z-]*\d)*\b")],
    "ip": [re.compile(r"\d{1,3}(?:\.\d{1,3}){3}")],
    "url": [re.compile(r"https?://\S+", re.IGNORECASE)],
}


@dataclass(frozen=True)
class Entity:
    type: str
    text: str
    start: int
    end: int


def extract_entities(text: str) -> list[Entity]:
    found: list[Entity] = []
    for etype, patterns in _ENTITY_RES.items():
        for pattern in patterns:
            for match in pattern.finditer(text):
                found.append(Entity(etype, match.group(0), match.start(), match.end()))
    return sorted(found, key=lambda e: (e.start, -e.end))


def dictionary_entities(text: str) -> list[Entity]:
    """业务词典实体：词典词条在文本中的出现。"""
    stopwords = load_stopwords()
    found: list[Entity] = []
    lowered = text.lower()
    for term in _dictionary_terms():
        if term in stopwords or len(term) < 2:
            continue
        start = 0
        while (idx := lowered.find(term, start)) != -1:
            found.append(Entity("dict", term, idx, idx + len(term)))
            start = idx + len(term)
    return found


@lru_cache(maxsize=1)
def _dictionary_terms() -> tuple[str, ...]:
    from app.nlp.tokenizer import DICT_DIR

    terms: list[str] = []
    if DICT_DIR.is_dir():
        for path in sorted(DICT_DIR.glob("*.dict")):
            for line in path.read_text(encoding="utf-8").splitlines():
                word = line.split()[0].strip() if line.strip() else ""
                if word and not word.startswith("#"):
                    terms.append(word.lower())
    return tuple(terms)


def entity_signature(text: str) -> frozenset[tuple[str, str]]:
    """实体签名：用于近重复保护比较（金额/日期/版本等变化不得视为重复）。"""
    return frozenset((e.type, e.text) for e in extract_entities(text))
