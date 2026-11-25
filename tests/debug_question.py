"""单题探针：打印一道题的检索各通道命中与 /qa 实际回答，用于定位管线问题。

用法：
    uv run python tests/debug_question.py "小儿原发性腹膜炎的护理是什么？" [--kb huatuo|cmedqa]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.eval_medical import DATA, load_state, login  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("question")
    ap.add_argument("--kb", default="huatuo")
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--top-k", type=int, default=6)
    ap.add_argument("--username", default="med_eval")
    ap.add_argument("--password", default="MedEval!2026")
    ap.add_argument("--skip-qa", action="store_true")
    args = ap.parse_args()

    client = login(args.base, args.username, args.password)
    kb_id = load_state().get(f"{args.kb}_kb_id")
    if not kb_id:
        sys.exit("缺 kb_state，先 ingest")

    dbg = client.post("/retrieval/debug",
                      json={"kb_id": kb_id, "question": args.question, "top_k": args.top_k})
    print(f"mode={dbg['mode']} status={dbg['status']} 查询扩展: {dbg['query_expanded'][:60]}")
    for chan in ("lexical", "vector", "fused", "reranked", "evidence"):
        hits = dbg.get(chan) or []
        print(f"\n== {chan} ({len(hits)}) ==")
        for h in hits[: args.top_k]:
            title = h.get("title_path", "")
            content = h.get("content", "").replace("\n", " ")[:70]
            score = h.get("score")
            print(f"  {h['chunk_id']}  {score if score is not None else '':<8} {title[-30:]} | {content}")

    if not args.skip_qa:
        qa = client.post("/qa", json={"kb_id": kb_id, "question": args.question})
        print(f"\n== /qa status={qa['status']} ==")
        print((qa.get("answer") or "")[:1200])


if __name__ == "__main__":
    main()
