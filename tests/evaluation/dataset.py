"""评测集数据结构与加载（实现文档 §14.1）。

种子评测集（seed_eval_set.jsonl）用于演示与回归 harness 本身；
正式验收须扩展为 ≥200 题人工标注（≥40 无答案、≥20 版本/冲突），
调参集与验收集按文档/主题独立划分——标注工作需在真实语料上进行。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

SEED_DATASET = Path(__file__).parent / "seed_eval_set.jsonl"


@dataclass
class EvalCase:
    id: str
    question: str
    answerable: bool
    hints: list[str] = field(default_factory=list)  # 相关证据包含的原文子串
    tags: list[str] = field(default_factory=list)  # 如 version / condition


def load_cases(path: Path = SEED_DATASET) -> list[EvalCase]:
    cases: list[EvalCase] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        raw = json.loads(line)
        cases.append(
            EvalCase(
                id=raw["id"],
                question=raw["question"],
                answerable=raw["answerable"],
                hints=raw.get("hints", []),
                tags=raw.get("tags", []),
            )
        )
    return cases


def validate_dataset(cases: list[EvalCase]) -> list[str]:
    """数据集自检：返回问题列表（为空表示通过）。"""
    problems: list[str] = []
    ids = [c.id for c in cases]
    if len(ids) != len(set(ids)):
        problems.append("存在重复 id")
    for case in cases:
        if case.answerable and not case.hints:
            problems.append(f"{case.id}: 可回答题缺少证据 hints")
        if not case.answerable and case.hints:
            problems.append(f"{case.id}: 无答案题不应有 hints")
    return problems
