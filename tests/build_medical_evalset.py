"""医疗问答系统测试集构建脚本（数据来源 data/medical/raw/，输出 tests/data/medical/）。

原始数据来源（均走 hf-mirror.com，获取命令见 tests/data/medical/README.md）：
- FreedomIntelligence/huatuo_encyclopedia_qa  华佗百科问答（开放问答，长答案）
- C-MTEB/CmedqaRetrieval(+qrels)              cMedQA 医疗检索评测三元组（10w 语料/3999 查询/qrels）
- lmlmcat/cmmlu                                CMMLU 医学子集（中文选择题）

用法：
    uv run python tests/build_medical_evalset.py
"""

from __future__ import annotations

import ast
import csv
import json
import random
from pathlib import Path

import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "medical" / "raw"
OUT = ROOT / "tests" / "data" / "medical"

# CMMLU 中医学相关子集（解剖/临床知识/医学统计/预防营养/内科学/中医等）
CMMLU_MEDICAL_SUBJECTS = [
    "anatomy",
    "clinical_knowledge",
    "college_medicine",
    "college_medical_statistics",
    "genetics",
    "nutrition",
    "professional_medicine",
    "traditional_chinese_medicine",
]

CORPUS_EVAL_SIZE = 10000  # 检索评测语料抽样规模（全部金标文档 + 随机干扰）
DOCS_PER_KB_FILE = 250   # 知识库 md 分片：每文件文档数（控制单文件在几 MB 内）
SEED = 42


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _py(value):
    """字段可能是真 JSON 数组，也可能是字符串化的 Python 列表。"""
    return ast.literal_eval(value) if isinstance(value, str) else value


def load_huatuo(path: Path) -> list[dict]:
    """华佗百科问答：questions 是“同义改写组”的列表，answers 是百科式长答案。"""
    items = []
    for line in path.read_text(encoding="utf-8").splitlines():
        obj = json.loads(line)
        q_groups = _py(obj["questions"])
        answers = _py(obj["answers"])
        for i, qg in enumerate(q_groups):
            variants = qg if isinstance(qg, list) else [qg]
            ans = answers[i] if i < len(answers) else ""
            if isinstance(ans, list):
                ans = ans[0] if ans else ""
            if variants and ans.strip():
                items.append({"questions": variants, "gold_answer": ans.strip()})
    return items


def build_open_qa() -> dict[str, int]:
    counts = {}
    for split, src in [("test", "huatuo_enc_test.jsonl"), ("dev", "huatuo_enc_val.jsonl")]:
        items = load_huatuo(RAW / src)
        rows = [
            {"id": f"huatuo-enc-{split}-{i:05d}", "questions": it["questions"],
             "gold_answer": it["gold_answer"], "source": "FreedomIntelligence/huatuo_encyclopedia_qa"}
            for i, it in enumerate(items)
        ]
        write_jsonl(OUT / f"qa_open_{split}.jsonl", rows)
        counts[f"qa_open_{split}"] = len(rows)
    return counts


def build_mcq() -> int:
    rows = []
    for subject in CMMLU_MEDICAL_SUBJECTS:
        path = RAW / "cmmlu" / "test" / f"{subject}.csv"
        if not path.exists():
            continue
        with path.open(encoding="utf-8", newline="") as f:
            for i, rec in enumerate(csv.DictReader(f)):
                rec = {k.lower().strip(): v for k, v in rec.items() if k}
                options = {K: rec[K.lower()] for K in "ABCD" if rec.get(K.lower())}
                answer = (rec.get("answer") or "").strip().upper()
                if not rec.get("question") or answer not in "ABCD":
                    continue
                rows.append({"id": f"cmmlu-{subject}-{i:04d}", "subject": subject,
                             "question": rec["question"].strip(), "options": options,
                             "answer": answer, "source": "lmlmcat/cmmlu"})
    write_jsonl(OUT / "mcq_zh.jsonl", rows)
    return len(rows)


def build_retrieval_and_kb() -> dict:
    corpus = pq.read_table(RAW / "cmedqa_corpus.parquet").to_pylist()
    queries = pq.read_table(RAW / "cmedqa_queries.parquet").to_pylist()
    qrels = [
        {"qid": r["qid"], "pid": r["pid"], "score": int(r["score"])}
        for r in pq.read_table(RAW / "cmedqa_qrels.parquet").to_pylist()
    ]

    text_of = {c["id"]: c["text"].strip() for c in corpus}
    gold_pids = {r["pid"] for r in qrels if r["score"] >= 1 and r["pid"] in text_of}

    # 评测语料 = 全部金标文档 + 随机干扰文档（固定种子，可复现）
    rng = random.Random(SEED)
    distractors = [pid for pid in text_of if pid not in gold_pids]
    rng.shuffle(distractors)
    eval_pids = sorted(gold_pids) + distractors[: max(0, CORPUS_EVAL_SIZE - len(gold_pids))]

    (OUT / "retrieval").mkdir(parents=True, exist_ok=True)
    write_jsonl(OUT / "retrieval" / "queries.jsonl",
                [{"id": q["id"], "text": q["text"].strip()} for q in queries if q["text"].strip()])
    write_jsonl(OUT / "retrieval" / "qrels.jsonl", qrels)
    write_jsonl(OUT / "retrieval" / "corpus_eval.jsonl",
                [{"pid": pid, "text": text_of[pid]} for pid in eval_pids])

    # 知识库分片 md：cMedQA 语料（按 pid 标题分块，金标可经文本包含关系回判）
    kb_cmedqa = OUT / "kb" / "cmedqa"
    kb_cmedqa.mkdir(parents=True, exist_ok=True)
    for old in kb_cmedqa.glob("*.md"):
        old.unlink()
    # 文件名带源前缀：对象 key 是（日期,用户,文件名）粒度，跨库同名同日上传会冲突
    for part, start in enumerate(range(0, len(eval_pids), DOCS_PER_KB_FILE)):
        block = eval_pids[start:start + DOCS_PER_KB_FILE]
        body = "\n".join(f"## {pid}\n\n{text_of[pid]}\n" for pid in block)
        (kb_cmedqa / f"cmedqa-part-{part:03d}.md").write_text(
            f"# 医疗问答语料库-cMedQA-{part:03d}\n\n{body}", encoding="utf-8", newline="\n")

    # 知识库分片 md：华佗百科答案（开放问答端到端用，金标文档=百科条目答案）
    kb_huatuo = OUT / "kb" / "huatuo"
    kb_huatuo.mkdir(parents=True, exist_ok=True)
    for old in kb_huatuo.glob("*.md"):
        old.unlink()
    items = load_huatuo(RAW / "huatuo_enc_test.jsonl")
    for part, start in enumerate(range(0, len(items), 100)):
        block = items[start:start + 100]
        body = "\n".join(f"## {it['questions'][0]}\n\n{it['gold_answer']}\n" for it in block)
        (kb_huatuo / f"huatuo-part-{part:03d}.md").write_text(
            f"# 医疗百科知识库-{part:03d}\n\n{body}", encoding="utf-8", newline="\n")

    return {"retrieval_queries": len(queries), "qrels": len(qrels),
            "gold_docs": len(gold_pids), "corpus_eval": len(eval_pids),
            "kb_cmedqa_files": len(list(kb_cmedqa.glob('part-*.md'))),
            "kb_huatuo_items": len(items), "kb_huatuo_files": len(list(kb_huatuo.glob('part-*.md')))}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    manifest = {}
    manifest.update(build_open_qa())
    manifest["mcq_zh"] = build_mcq()
    manifest.update(build_retrieval_and_kb())
    (OUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                       encoding="utf-8", newline="\n")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    print(f"\n已输出到 {OUT}")


if __name__ == "__main__":
    main()
