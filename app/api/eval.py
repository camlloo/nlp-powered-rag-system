"""评测结果只读聚合（管理端「模型评测」页数据源）。

数据源为 tests/eval_medical.py 落在 settings.eval_results_dir 的
JSON/JSONL 产物文件：不落库、不缓存，文件缺失一律返回 null（页面显示"未运行"）。
mcq 轨道的汇总原本只打印在 stdout，这里从明细重算并额外披露"给出选项者"口径。
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter

from app.api.deps import AdminDep
from app.core.config import get_settings
from app.core.errors import NotFound

router = APIRouter(tags=["eval"])

_DETAIL_TRACKS = {
    "qa": ("qa_open.jsonl", 2000),
    "preference": ("preference.jsonl", 2000),
    "mcq": ("mcq.jsonl", 600),
}


def _results_dir() -> Path:
    root = Path(__file__).resolve().parents[2]
    path = (root / get_settings().eval_results_dir).resolve()
    if not path.is_relative_to(root):  # 防 env 配置越界读取仓库外文件
        raise NotFound("评测结果目录配置无效")
    return path


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _read_jsonl(path: Path):
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    rows = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _retrieval_summary(directory: Path, source: str):
    data = _read_json(directory / f"retrieval_{source}.json")
    return (data or {}).get("summary") if data else None


def _mcq_stats(rows):
    if not rows:
        return None
    total = len(rows)
    answered = [r for r in rows if r.get("picked")]
    by_subject: dict[str, list[int]] = {}
    for r in rows:
        counter = by_subject.setdefault(r.get("subject") or "?", [0, 0])
        counter[0] += 1 if r.get("ok") else 0
        counter[1] += 1
    return {
        "n": total,
        "overall_acc": round(sum(1 for r in rows if r.get("ok")) / total, 4),
        "answered": len(answered),
        "no_option": total - len(answered),
        "answered_acc": (
            round(sum(1 for r in answered if r.get("ok")) / len(answered), 4)
            if answered
            else None
        ),
        "by_subject": {
            k: {"correct": v[0], "total": v[1]} for k, v in sorted(by_subject.items())
        },
    }


@router.get("/eval/overview")
def eval_overview(_: AdminDep) -> dict:
    directory = _results_dir()
    return {
        "retrieval": {
            "cmedqa": _retrieval_summary(directory, "cmedqa"),
            "huatuo": _retrieval_summary(directory, "huatuo"),
        },
        "qa": _read_json(directory / "qa_summary.json"),
        "ragas": _read_json(directory / "ragas_summary.json"),
        "mcq": _mcq_stats(_read_jsonl(directory / "mcq.jsonl")),
        "preference": {
            "no_reference": _read_json(directory / "preference_summary.json"),
            "with_reference": _read_json(directory / "preference_with_gold_summary.json"),
        },
        "system_perf": _read_json(directory / "system_perf.json"),
        "meta": {
            "manifest": _read_json(directory.parent / "manifest.json"),
            "files_present": (
                sorted(p.name for p in directory.glob("*") if p.is_file())
                if directory.is_dir()
                else []
            ),
        },
    }


@router.get("/eval/details/{track}")
def eval_details(track: str, _: AdminDep) -> dict:
    entry = _DETAIL_TRACKS.get(track)
    if entry is None:
        raise NotFound(f"未知评测轨道：{track}")
    filename, answer_cap = entry
    rows = _read_jsonl(_results_dir() / filename)
    if rows is None:
        return {"track": track, "rows": None}
    for row in rows:
        for key in ("answer", "rag_answer", "bare_answer"):
            value = row.get(key)
            if isinstance(value, str) and len(value) > answer_cap:
                row[key] = value[:answer_cap] + "…"
    return {"track": track, "rows": rows}
