"""评测执行器：对评测集逐题跑检索并产出 CaseResult 列表（实现文档 §14.2）。"""

from __future__ import annotations

import time

from app.indexing.cache import LoadedIndex
from app.retrieval.service import RetrievalService
from tests.evaluation.dataset import EvalCase
from tests.evaluation.metrics import CaseResult, is_relevant


async def run_evaluation(
    service: RetrievalService,
    cases: list[EvalCase],
    *,
    kb_id: int,
    tenant_id: int,
    loaded: LoadedIndex | None = None,
    top_k: int = 6,
) -> list[CaseResult]:
    """逐题检索；loaded 提供全库相关证据计数（Recall 分母），缺省时 Recall 不计。"""
    results: list[CaseResult] = []
    for case in cases:
        started = time.monotonic()
        result = await service.retrieve(
            kb_id=kb_id, tenant_id=tenant_id, question=case.question, top_k=top_k
        )
        latency_ms = int((time.monotonic() - started) * 1000)
        ranked = [block.content for block in result.evidence[:10]]

        relevant_total = 0
        if loaded is not None and case.hints:
            relevant_total = sum(
                1
                for record in loaded.records.values()
                if is_relevant(record.content, case.hints)
            )
        relevant_in_top5 = sum(1 for content in ranked[:5] if is_relevant(content, case.hints))
        first_rank = next(
            (i for i, content in enumerate(ranked, start=1) if is_relevant(content, case.hints)),
            0,
        )
        results.append(
            CaseResult(
                case_id=case.id,
                answerable=case.answerable,
                status=result.status,
                latency_ms=latency_ms,
                ranked_contents=ranked,
                relevant_total=relevant_total,
                relevant_in_top5=relevant_in_top5,
                first_relevant_rank_10=first_rank,
            )
        )
    return results
