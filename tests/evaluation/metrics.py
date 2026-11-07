"""评测指标（实现文档 §14.2；报告必须带分母与样本量）。"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field


@dataclass
class CaseResult:
    case_id: str
    answerable: bool
    status: str
    latency_ms: int
    ranked_contents: list[str] = field(default_factory=list)  # 前 10 条证据内容
    relevant_total: int = 0  # 全库相关证据数（按 hints 匹配；无标注基线时为 0）
    relevant_in_top5: int = 0
    first_relevant_rank_10: int = 0  # 1 起始；0 表示前 10 未命中

    @property
    def rejected(self) -> bool:
        return self.status == "insufficient_evidence"


def is_relevant(content: str, hints: list[str]) -> bool:
    return any(hint and hint in content for hint in hints)


def aggregate(results: list[CaseResult]) -> dict:
    answerable = [r for r in results if r.answerable]
    unanswerable = [r for r in results if not r.answerable]

    def mean(values: list[float]) -> float | None:
        return round(statistics.mean(values), 4) if values else None

    recall_values = [
        r.relevant_in_top5 / r.relevant_total
        for r in answerable
        if r.relevant_total > 0
    ]
    hit_values = [1.0 if r.relevant_in_top5 > 0 else 0.0 for r in answerable]
    mrr_values = [
        1.0 / r.first_relevant_rank_10 if r.first_relevant_rank_10 else 0.0
        for r in answerable
    ]
    latencies = sorted(r.latency_ms for r in results)
    p95 = latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))] if latencies else None
    return {
        "sample_size": len(results),
        "n_answerable": len(answerable),
        "n_unanswerable": len(unanswerable),
        "recall_at_5": mean(recall_values),
        "recall_denominator": len(recall_values),
        "hit_at_5": mean(hit_values),
        "mrr_at_10": mean(mrr_values),
        "rejection_rate": (
            round(sum(1 for r in unanswerable if r.rejected) / len(unanswerable), 4)
            if unanswerable
            else None
        ),
        "false_rejection_rate": (
            round(sum(1 for r in answerable if r.rejected) / len(answerable), 4)
            if answerable
            else None
        ),
        "p95_latency_ms": p95,
    }


def format_report(mode: str, agg: dict) -> str:
    lines = [
        f"## 评测报告（mode={mode}）",
        f"- 样本量：{agg['sample_size']}"
        f"（可回答 {agg['n_answerable']} / 无答案 {agg['n_unanswerable']}）",
        f"- Recall@5 = {agg['recall_at_5']}（分母 {agg['recall_denominator']}，目标 ≥0.85）",
        f"- Hit@5 = {agg['hit_at_5']}（目标 ≥0.90）",
        f"- MRR@10 = {agg['mrr_at_10']}（目标 ≥0.75）",
        f"- 无答案拒答率 = {agg['rejection_rate']}（目标 ≥0.90）",
        f"- 可回答误拒率 = {agg['false_rejection_rate']}（目标 ≤0.10）",
        f"- 检索 P95 延迟 = {agg['p95_latency_ms']} ms（目标 ≤800，不含向量远程调用）",
        "",
        "注：以上为占位阈值下的观测值；正式验收须以真实语料与 ≥200 题标注集跑分（§14.1/§14.2）。",
    ]
    return "\n".join(lines)
