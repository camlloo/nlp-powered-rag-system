"""医疗问答系统评测脚本（对 tests/data/medical/ 测试集跑真实管线）。

依赖：先启动后端（WSL docker 里的 pgvector + `uv run uvicorn app.main:app`），
评测通过 HTTP 调 /api/v1 的检索/QA 接口，不绕过任何线上逻辑。

子命令：
    ingest   建库并上传知识库语料（kb/cmedqa 或 kb/huatuo 分片 md）
    retrieve 检索评测：recall@1/5/10 + MRR@10（cmedqa 用 qrels；huatuo 用金标答案包含判定）
    qa       端到端开放问答：调 /qa，LLM judge 对照参考答案给二元分 + 0-5 五维分
             （准确性/完整性/逻辑性/专业性/安全性），另算答案-金标余弦相似度与耗时
    mcq      选择题：CMMLU 医学子集走 /qa，抽取选项字母算准确率
    ragas    RAGAS 指标化评测：faithfulness / answer_relevancy /
             context_precision / context_recall（huatuo 走真实管线；
             synthetic 为离线自检模式，不需要后端）
    preference 盲评胜出率（借鉴王永刚论文表 4-4~4-6）：本系统 RAG 回答 vs
             同底座裸模型直答，裁判对五维+综合逐维选优，统计分维胜出率

所有走接口的命令同时记录客户端耗时与成功率，合并写入 results/system_perf.json
（借鉴论文 §5.6.3 性能效率指标）。

示例：
    uv run python tests/eval_medical.py ingest --source all
    uv run python tests/eval_medical.py retrieve --source cmedqa --limit 200
    uv run python tests/eval_medical.py qa --limit 30
    uv run python tests/eval_medical.py mcq --limit 100
    uv run python tests/eval_medical.py ragas --limit 20
    uv run python tests/eval_medical.py ragas --source synthetic --limit 3   # 自检
    uv run python tests/eval_medical.py preference --limit 50
"""

from __future__ import annotations

import argparse
import json
import math
import random
import re
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # 直接运行脚本时保证可 import app.*
    sys.path.insert(0, str(ROOT))
DATA = ROOT / "tests" / "data" / "medical"
STATE_FILE = DATA / ".kb_state.json"
RESULTS = DATA / "results"

DEFAULT_BASE = "http://127.0.0.1:8000"
API = "/api/v1"
DEFAULT_USER = "med_eval"
DEFAULT_PASS = "MedEval!2026"
KB_NAMES = {"cmedqa": "医疗问答评测库-cMedQA", "huatuo": "医疗百科评测库-华佗"}

SEED = 42


# ---------------------------------------------------------------- 基础工具

def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def write_jsonl(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def norm(text: str) -> str:
    """去空白/标点差异，用于金标文档文本包含判定。"""
    return re.sub(r"[\s，。！？；：、,.!?;:（）()\[\]【】\"'“”‘’]+", "", text)


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


class Client:
    def __init__(self, base: str, token: str):
        self.base = base.rstrip("/")
        self.http = httpx.Client(timeout=120.0, headers={"Authorization": f"Bearer {token}"})

    def url(self, path: str) -> str:
        return f"{self.base}{API}{path}"

    def post(self, path: str, **kw):
        r = self.http.post(self.url(path), **kw)
        r.raise_for_status()
        return r.json()

    def timed_post(self, path: str, section: str, **kw):
        """POST 并记录客户端侧耗时/成败（论文式性能效率指标口径）。"""
        t0 = time.perf_counter()
        try:
            r = self.http.post(self.url(path), **kw)
            r.raise_for_status()
            record_perf(section, (time.perf_counter() - t0) * 1000, ok=True)
            return r.json()
        except Exception:
            record_perf(section, (time.perf_counter() - t0) * 1000, ok=False)
            raise

    def get(self, path: str):
        r = self.http.get(self.url(path))
        r.raise_for_status()
        return r.json()


def timed_post_retry(client: Client, path: str, section: str, attempts: int = 3, **kw):
    """带瞬时故障（连接重置/5xx）重试的 POST，返回 (body|None, error_str)；
    重试耗尽不再让单条接口失败击穿整轨。"""
    err = ""
    for i in range(attempts):
        try:
            return client.timed_post(path, section, **kw), ""
        except httpx.RequestError as exc:
            resp = getattr(exc, "response", None)
            err = f"<HTTP {resp.status_code}>" if resp is not None else f"<{type(exc).__name__}>"
            if i < attempts - 1:
                time.sleep(3)
    return None, err


# ---------------------------------------------------------------- 工程指标（借鉴论文 §5.6.3）

_perf_times: dict[str, list[float]] = {}
_perf_fails: dict[str, int] = {}


def record_perf(section: str, ms: float, ok: bool = True) -> None:
    _perf_times.setdefault(section, [])
    if ok:
        _perf_times[section].append(ms)
    else:
        _perf_fails[section] = _perf_fails.get(section, 0) + 1


def flush_perf() -> None:
    """把本次运行的耗时/成功率统计合并进 results/system_perf.json（同名节覆盖）。"""
    RESULTS.mkdir(parents=True, exist_ok=True)
    path = RESULTS / "system_perf.json"
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    for section, times in _perf_times.items():
        n_ok, n_fail = len(times), _perf_fails.get(section, 0)
        data[section] = {
            "n_ok": n_ok, "n_fail": n_fail,
            "success_rate": round(n_ok / (n_ok + n_fail), 4) if n_ok + n_fail else None,
            "avg_ms": round(sum(times) / n_ok, 1) if n_ok else None,
            "min_ms": round(min(times), 1) if n_ok else None,
            "max_ms": round(max(times), 1) if n_ok else None,
        }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print("工程指标写入", path)


def login(base: str, username: str, password: str) -> Client:
    http = httpx.Client(timeout=30.0)
    r = http.post(f"{base}{API}/auth/login", json={"username": username, "password": password})
    if r.status_code in (401, 403, 404):
        r = http.post(f"{base}{API}/auth/register",
                      json={"username": username, "password": password,
                            "display_name": "医疗评测"})
    r.raise_for_status()
    return Client(base, r.json()["access_token"])


# ---------------------------------------------------------------- ingest

def cmd_ingest(args) -> None:
    client = login(args.base, args.username, args.password)
    state = load_state()
    kbs = client.get("/kbs").get("items", [])
    for source in (["cmedqa", "huatuo"] if args.source == "all" else [args.source]):
        kb_id = state.get(f"{source}_kb_id")
        if kb_id and kb_id not in {kb["kb_id"] for kb in kbs}:
            kb_id = None  # 缓存的 kb 可能已在服务端被删除
        if not kb_id:
            name = KB_NAMES[source]
            # 优先复用已有同名/带后缀的活库；软删除会永久占用 (tenant,name)，
            # 因此创建时依次尝试基础名与 -r2..-r9 后缀（记忆坑：撞名直接 500）
            reuse = [kb for kb in kbs if kb["name"] == name
                     or kb["name"].startswith(f"{name}-r")]
            if reuse:
                kb_id = reuse[0]["kb_id"]
            else:
                for try_name in [name] + [f"{name}-r{i}" for i in range(2, 10)]:
                    try:
                        kb_id = client.post("/kbs", json={"name": try_name})["kb_id"]
                        break
                    except httpx.HTTPStatusError:
                        continue
                if not kb_id:
                    sys.exit(f"创建知识库失败：{name} 及其后缀名均被占用")
        print(f"[{source}] kb_id={kb_id}")
        job_ids = []
        files = sorted((DATA / "kb" / source).glob("*.md"))
        if args.max_files:
            files = files[: args.max_files]
        for i, f in enumerate(files):
            with f.open("rb") as fh:
                resp = client.http.post(
                    client.url(f"/kbs/{kb_id}/documents"),
                    files={"file": (f.name, fh, "text/markdown")},
                    data={"visibility": "private"},
                )
            resp.raise_for_status()
            body = resp.json()
            if body.get("job_id"):
                job_ids.append(body["job_id"])
            print(f"  uploaded {i + 1}/{len(files)} {f.name} (job={body.get('job_id') or 'dup'})")
        # 等索引任务收敛（worker 高负载时连接可能瞬断，轮询失败只重试不退出）
        pending, failed = set(job_ids), set()
        while pending:
            done_now = set()
            for jid in pending:
                try:
                    st = client.get(f"/jobs/{jid}")["status"]
                except (httpx.HTTPError, OSError) as exc:
                    print(f"  查询 job {jid} 失败，稍后重试: {type(exc).__name__}")
                    continue
                if st == "succeeded":
                    done_now.add(jid)
                elif st == "failed":
                    failed.add(jid)
                    done_now.add(jid)
            pending -= done_now
            if pending:
                time.sleep(5)
        print(f"[{source}] indexing done, failed_jobs={len(failed)}")
        state[f"{source}_kb_id"] = kb_id
        save_state(state)
    print("kb id 已写入", STATE_FILE)


# ---------------------------------------------------------------- retrieve

def ranked_hit_lists(resp: dict) -> list[list[str]]:
    """最终用于生成的有序命中列表。仅 evidence 带 content 可用于文本包含判定；
    reranked/fused 等通道项只有 chunk_id/score，退回会导致空串误判 rank-1 命中。
    evidence 为空 = 系统给生成器的证据集为空，按未命中计。"""
    return [norm(h.get("content", "")) for h in (resp.get("evidence") or [])]


def cmd_retrieve(args) -> None:
    client = login(args.base, args.username, args.password)
    state = load_state()
    kb_id = state.get(f"{args.source}_kb_id")
    if not kb_id:
        sys.exit("请先执行 ingest 建库（缺少 .kb_state.json 中的 kb_id）")

    if args.source == "cmedqa":
        queries = read_jsonl(DATA / "retrieval" / "queries.jsonl")
        qrels = read_jsonl(DATA / "retrieval" / "qrels.jsonl")
        gold_text: dict[str, set[str]] = {}
        for r in qrels:
            if r["score"] >= 1:
                gold_text.setdefault(r["qid"], set())
        corpus = {c["pid"]: norm(c["text"])
                  for c in read_jsonl(DATA / "retrieval" / "corpus_eval.jsonl")}
        tasks = []
        for q in queries:
            texts = {corpus[r["pid"]] for r in qrels
                     if r["qid"] == q["id"] and r["score"] >= 1 and r["pid"] in corpus}
            if texts:
                tasks.append({"id": q["id"], "question": q["text"], "gold": texts})
    else:
        tasks = [
            {"id": it["id"], "question": it["questions"][0], "gold": {norm(it["gold_answer"])}}
            for it in read_jsonl(DATA / "qa_open_test.jsonl")
        ]

    rng = random.Random(SEED)
    sample = rng.sample(tasks, min(args.limit, len(tasks)))
    Ks = (1, 3, 5, 10)
    details = []
    p_sum = {k: 0.0 for k in Ks}
    r_sum = {k: 0.0 for k in Ks}
    top1_cos = []
    for n, t in enumerate(sample, 1):
        resp = client.timed_post("/retrieval/debug", "retrieve./retrieval/debug",
                                 json={"kb_id": kb_id, "question": t["question"], "top_k": 10})
        lists = ranked_hit_lists(resp)
        ranks = [next((i for i, c in enumerate(lists, 1) if gold in c or c in gold), 0)
                 for gold in t["gold"]]
        best = min([r for r in ranks if r], default=0)
        # P@k / R@k / F1@k：多金标(qrels)时三者才真正分离；单金标退化为命中数/截断
        for k in Ks:
            hits_k = sum(1 for r in ranks if 0 < r <= k)
            recall_k = hits_k / len(ranks) if ranks else 0.0
            prec_k = hits_k / k
            r_sum[k] += recall_k
            p_sum[k] += prec_k
        # 余弦相似度：向量通道 Top1 相似度（通道项无 content，金标块相似度改由 qa 轨的
        # answer-gold 余弦覆盖，此处不再从 vector 猜金标命中，避免空串误判）
        vec = resp.get("vector") or []
        if vec:
            top1_cos.append(max(h.get("cosine") or h.get("score") or 0.0 for h in vec[:10]))
        details.append({"id": t["id"], "question": t["question"], "best_rank": best})
        if n % 20 == 0:
            print(f"  {n}/{len(sample)} done")

    m = len(sample)
    summary = {"source": args.source, "n": m}
    for k in Ks:
        pr, rr_ = p_sum[k] / m, r_sum[k] / m
        f1 = 2 * pr * rr_ / (pr + rr_) if pr + rr_ else 0.0
        summary[f"precision@{k}"] = round(pr, 4)
        summary[f"recall@{k}"] = round(rr_, 4)
        summary[f"f1@{k}"] = round(f1, 4)
    rr_all = sum(1 / d["best_rank"] for d in details if d["best_rank"]) / m if details else 0
    summary["mrr@10"] = round(rr_all, 4)
    if top1_cos:
        summary["cosine_top1_mean"] = round(sum(top1_cos) / len(top1_cos), 4)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"retrieval_{args.source}.json"
    out.write_text(json.dumps({"summary": summary, "details": details},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print("明细写入", out)
    flush_perf()


# ---------------------------------------------------------------- qa (judge)

DIM5 = ("准确性", "完整性", "逻辑性", "专业性", "安全性")

JUDGE_PROMPT = """你是医疗问答系统评测员。请对照参考答案评估系统回答。

问题：{question}
参考答案：{gold}
系统回答：{pred}

要求：
1. score（二元分）：1=与参考答案医学事实一致；0.5=部分正确或有小误；0=错误或拒答；
2. dims（0-5 分制，5 最优，整数）：{dims_def}；
只输出 JSON：{{"score": 1, "dims": {{"准确性": 5, "完整性": 4, "逻辑性": 5, "专业性": 5, "安全性": 5}}, "reason": "一句话理由"}}"""

DIMS_DEF = ("准确性=无医学错误/幻觉，完整性=覆盖参考答案要点无关键缺失，"
            "逻辑性=推理与结构清晰，专业性=术语规范表述严谨，"
            "安全性=无危险或误导性建议并含必要提醒")


def make_judge():
    from langchain_openai import ChatOpenAI

    from app.core.config import get_settings

    s = get_settings()
    # 与生产网关一致（enable_thinking=False，实现文档 §8.3）；裁判/裸基线统一参数
    return ChatOpenAI(model=s.chat_model, base_url=s.model_base_url,
                      api_key=s.dashscope_api_key,
                      extra_body={"enable_thinking": False},
                      max_tokens=s.chat_max_output_tokens), s


def ask_judge(llm, prompt: str, retries: int = 2) -> dict:
    """让裁判输出单 JSON；调用异常（上游瞬断）与解析失败均重试，
    仍失败返回 parse_error，不让单条失败击穿整轨。"""
    last_raw = ""
    for _ in range(retries + 1):
        try:
            last_raw = str(llm.invoke(prompt).content)
        except Exception as exc:  # noqa: BLE001 - 百炼偶发连接重置
            last_raw = f"<invoke error {type(exc).__name__}>"
            time.sleep(2)
            continue
        m2 = re.search(r"\{.*\}", last_raw, re.S)
        if m2:
            try:
                obj = json.loads(m2.group(0))
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                pass
    return {"parse_error": last_raw[:200]}


def _coerce_dims(raw_dims) -> dict:
    out = {}
    for k in DIM5:
        try:
            out[k] = float((raw_dims or {}).get(k))
        except (TypeError, ValueError):
            pass
    return out


def make_embeddings():
    from langchain_openai import OpenAIEmbeddings

    from app.core.config import get_settings

    s = get_settings()
    return OpenAIEmbeddings(model=s.embedding_model, base_url=s.model_base_url,
                            api_key=s.dashscope_api_key,
                            check_embedding_ctx_length=False)


def embed_texts(emb, texts: list[str], chunk: int = 10) -> list[list[float]]:
    """百炼 embedding 端点单请求批上限 10（超批 400 InvalidParameter），手动分块。"""
    out: list[list[float]] = []
    for i in range(0, len(texts), chunk):
        out.extend(emb.embed_documents(texts[i:i + chunk]))
    return out


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def cmd_qa(args) -> None:
    client = login(args.base, args.username, args.password)
    kb_id = load_state().get("huatuo_kb_id")
    if not kb_id:
        sys.exit("请先执行 ingest --source huatuo 建库")
    llm, _ = make_judge()

    items = read_jsonl(DATA / "qa_open_test.jsonl")
    sample = random.Random(SEED).sample(items, min(args.limit, len(items)))
    scores, details, parse_fail = [], [], 0
    for n, it in enumerate(sample, 1):
        question = it["questions"][0]
        body, err = timed_post_retry(client, "/qa", "qa./qa",
                                     json={"kb_id": kb_id, "question": question})
        pred = (body or {}).get("answer") or err
        if args.no_judge:
            score, dims, reason = None, {}, ""
        else:
            verdict = ask_judge(llm, JUDGE_PROMPT.format(question=question,
                                                         gold=it["gold_answer"],
                                                         pred=pred, dims_def=DIMS_DEF))
            if "parse_error" in verdict:
                parse_fail += 1
                score, dims, reason = 0.0, {}, f"judge 输出无法解析: {verdict['parse_error']}"
            else:
                score = float(verdict.get("score", 0))
                dims = _coerce_dims(verdict.get("dims"))
                reason = verdict.get("reason", "")
        if score is not None:
            scores.append(score)
        details.append({"id": it["id"], "question": question, "answer": pred,
                        "score": score, "dims": dims, "reason": reason})
        print(f"  [{n}/{len(sample)}] score={score} {question[:30]}")

    # 答案-金标余弦相似度（借鉴论文“余弦相似度”客观指标）：批量向量化，失败不致命
    if not args.no_cosine:
        try:
            emb = make_embeddings()
            idx = [i for i, d in enumerate(details) if d["answer"]]
            gold_vecs = embed_texts(emb, [sample[i]["gold_answer"] for i in idx])
            pred_vecs = embed_texts(emb, [details[i]["answer"] for i in idx])
            for j, i in enumerate(idx):
                details[i]["cosine_gold"] = round(cosine(pred_vecs[j], gold_vecs[j]), 4)
        except Exception as exc:  # noqa: BLE001 - 相似度是附加维度，失败只记录
            print("  金标余弦相似度计算失败:", str(exc)[:200])

    dim_means = {}
    for k in DIM5:
        vals = [d["dims"][k] for d in details if k in d.get("dims", {})]
        if vals:
            dim_means[k] = round(sum(vals) / len(vals), 2)
    cos_vals = [d["cosine_gold"] for d in details if d.get("cosine_gold") is not None]
    summary = {"n": len(sample), "judge_parse_fail": parse_fail,
               "mean_score": round(sum(scores) / len(scores), 4) if scores else None,
               "dim_means_0_5": dim_means or None,
               "cosine_gold_mean": round(sum(cos_vals) / len(cos_vals), 4) if cos_vals else None}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "qa_open.jsonl").write_text(
        "".join(json.dumps(d, ensure_ascii=False) + "\n" for d in details), encoding="utf-8")
    (RESULTS / "qa_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    flush_perf()


# ---------------------------------------------------------------- mcq

def cmd_mcq(args) -> None:
    client = login(args.base, args.username, args.password)
    state = load_state()
    kb_id = state.get(f"{args.kb_source}_kb_id") if args.kb_source != "none" else None
    items = read_jsonl(DATA / "mcq_zh.jsonl")
    sample = random.Random(SEED).sample(items, min(args.limit, len(items)))

    correct = {s: [0, 0] for s in sorted({it["subject"] for it in sample})}
    details = []
    for n, it in enumerate(sample, 1):
        options = "\n".join(f"{K}. {v}" for K, v in it["options"].items())
        question = (f"{it['question']}\n{options}\n"
                    "请先从 A/B/C/D 中给出唯一答案选项，再简要说明理由。")
        payload = {"question": question}
        if kb_id:
            payload["kb_id"] = kb_id
        body, err = timed_post_retry(client, "/qa", "mcq./qa", json=payload)
        answer = (body or {}).get("answer") or err
        match = (re.search(r"答案\s*[是为：:]*\s*([ABCD])", answer)
                 or re.search(r"\b([ABCD])\b", answer))
        picked = match.group(1) if match else ""
        ok = picked == it["answer"]
        correct[it["subject"]][0] += ok
        correct[it["subject"]][1] += 1
        details.append({"id": it["id"], "subject": it["subject"], "picked": picked,
                        "gold": it["answer"], "ok": ok, "answer": answer[:400]})
        if n % 10 == 0:
            got = sum(v[0] for v in correct.values())
            tot = sum(v[1] for v in correct.values())
            print(f"  [{n}/{len(sample)}] acc={got}/{tot}")

    total_c = sum(v[0] for v in correct.values())
    total_n = sum(v[1] for v in correct.values())
    summary = {"n": total_n, "overall_acc": round(total_c / total_n, 4) if total_n else None,
               "by_subject": {k: f"{v[0]}/{v[1]}" for k, v in sorted(correct.items())}}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    write_jsonl(RESULTS / "mcq.jsonl", details)
    flush_perf()


# ---------------------------------------------------------------- preference (盲评胜出率)

BASE_ANSWER_PROMPT = """你是一位具备丰富医学知识的专业助手。请准确、专业地回答下面的医疗问题，必要时给出就医与用药提醒。

问题：{question}"""

PREF_PROMPT = """你是医疗问答评测专家。针对同一问题有两个匿名回答，请对比评判。

问题：{question}
回答一：{a}
回答二：{b}

逐维度判断哪个回答更好：准确性（医学事实无误无幻觉）、完整性（要点覆盖全面）、
逻辑性（推理与结构清晰）、专业性（术语规范表述严谨）、安全性（无危险/误导建议）、
综合（整体质量）。两者差异不明显输出 tie。
只输出 JSON：{{"准确性": "一", "完整性": "二", "逻辑性": "tie", "专业性": "一", "安全性": "二", "综合": "一"}}"""

DIM6 = DIM5 + ("综合",)


def cmd_preference(args) -> None:
    """借鉴论文第 4 章主观实验：RAG 全管线 vs 同底座裸模型，盲评分维胜出率。"""
    client = login(args.base, args.username, args.password)
    kb_id = load_state().get("huatuo_kb_id")
    if not kb_id:
        sys.exit("请先执行 ingest --source huatuo 建库")
    llm, _ = make_judge()

    items = read_jsonl(DATA / "qa_open_test.jsonl")
    sample = random.Random(SEED).sample(items, min(args.limit, len(items)))  # 与 qa 轨同子集
    rng = random.Random(SEED + 1)
    wins = {d: {"rag": 0, "bare": 0, "tie": 0} for d in DIM6}
    details, parse_fail = [], 0

    for n, it in enumerate(sample, 1):
        question = it["questions"][0]
        # 候选 R：本系统全管线（检索 + 证据生成）
        body, err = timed_post_retry(client, "/qa", "preference./qa",
                                     json={"kb_id": kb_id, "question": question})
        rag_ans = (body or {}).get("answer") or err
        # 候选 B：同底座裸模型直答（无检索，参数与生产一致），连接故障重试
        bare_ans, t0 = "", time.perf_counter()
        for attempt in range(3):
            try:
                bare_ans = str(llm.invoke(BASE_ANSWER_PROMPT.format(question=question)).content)
                record_perf("preference.裸模型直答", (time.perf_counter() - t0) * 1000, ok=True)
                break
            except Exception as exc:  # noqa: BLE001
                if attempt == 2:
                    bare_ans = f"<模型调用失败 {type(exc).__name__}>"
                    record_perf("preference.裸模型直答", (time.perf_counter() - t0) * 1000, ok=False)
                else:
                    time.sleep(3)

        swapped = bool(rng.random() < 0.5)  # 随机左右位次，消除位置偏差
        a, b = (bare_ans, rag_ans) if swapped else (rag_ans, bare_ans)
        verdict = ask_judge(llm, PREF_PROMPT.format(question=question,
                                                    a=a[:1500], b=b[:1500]))
        row = {"id": it["id"], "question": question, "swapped": swapped,
               "rag_answer": rag_ans[:800], "bare_answer": bare_ans[:800], "dims": {}}
        if "parse_error" in verdict:
            parse_fail += 1
            row["parse_error"] = verdict["parse_error"]
        else:
            for d in DIM6:
                v = str(verdict.get(d, "tie")).strip().upper()
                one = ("一" in v) or v in ("1", "A")
                two = ("二" in v) or v in ("2", "B")
                if one:
                    side = "bare" if swapped else "rag"
                elif two:
                    side = "rag" if swapped else "bare"
                else:
                    side = "tie"
                wins[d][side] += 1
                row["dims"][d] = side
        details.append(row)
        print(f"  [{n}/{len(sample)}] {row.get('dims') or 'PARSE_FAIL'} {question[:26]}")

    m = len(sample)
    summary = {"n": m, "seed": SEED, "judge_parse_fail": parse_fail,
               "winrate_pct": {d: {k: round(v / m * 100, 1) for k, v in wins[d].items()}
                               for d in DIM6}}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "preference_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    write_jsonl(RESULTS / "preference.jsonl", details)
    flush_perf()


# ---------------------------------------------------------------- ragas


def _ragas_shim() -> None:
    """ragas 0.4.3 启动时 import langchain_community.chat_models.vertexai，
    该模块在 langchain-community 0.4 中已被移除；VertexAI 分支百炼场景永远走不到，
    补一个惰性 stub 即可复用其余全部功能（不改第三方包源码）。"""
    import types

    name = "langchain_community.chat_models.vertexai"
    try:
        import importlib

        importlib.import_module(name)
        return  # 旧版 langchain-community 自带，无需处理
    except ImportError:
        pass
    stub = types.ModuleType(name)

    class ChatVertexAI:
        def __init__(self, *a, **kw):
            raise RuntimeError("VertexAI 模型在本环境不可用（评测走百炼，不涉及）")

    stub.ChatVertexAI = ChatVertexAI  # type: ignore[attr-defined]
    sys.modules[name] = stub
    import langchain_community.llms as lc_llms

    if not hasattr(lc_llms, "VertexAI"):
        lc_llms.VertexAI = ChatVertexAI


def _ragas_models():
    """RAGAS 走项目同一套模型配置（百炼 OpenAI 兼容端点）。"""
    from langchain_openai import ChatOpenAI, OpenAIEmbeddings
    from ragas.embeddings import LangchainEmbeddingsWrapper
    from ragas.llms import LangchainLLMWrapper

    from app.core.config import get_settings

    s = get_settings()
    # enable_thinking=False：qwen3 思考模式会拖慢裁判调用，且与 RAGAS
    # answer_relevancy 的 n>1 采样请求冲突（百炼端点 400）
    chat = ChatOpenAI(model=s.chat_model, base_url=s.model_base_url,
                      api_key=s.dashscope_api_key,
                      extra_body={"enable_thinking": False})
    emb = OpenAIEmbeddings(model=s.embedding_model, base_url=s.model_base_url,
                           api_key=s.dashscope_api_key, check_embedding_ctx_length=False)
    # 注：本版本 langchain_openai 无批量参数；>10 条的 embed_documents 需走 embed_texts 分块
    return LangchainLLMWrapper(chat), LangchainEmbeddingsWrapper(emb)


def cmd_ragas(args) -> None:
    import warnings

    warnings.filterwarnings("ignore", category=DeprecationWarning)
    _ragas_shim()
    from ragas.dataset_schema import EvaluationDataset
    from ragas.evaluation import evaluate
    from ragas.metrics import (
        Faithfulness,
        LLMContextPrecisionWithReference,
        LLMContextRecall,
        ResponseRelevancy,
    )
    from ragas.run_config import RunConfig

    all_items = read_jsonl(DATA / "qa_open_test.jsonl")
    items = random.Random(SEED).sample(all_items, min(args.limit, len(all_items)))

    # 组装 RAGAS 样本：user_input / retrieved_contexts / response / reference
    rows = []
    if args.source == "synthetic":
        # 离线自检：金标答案同时充当上下文与回答，四项指标都应接近满分
        for it in items:
            rows.append({"user_input": it["questions"][0],
                         "retrieved_contexts": [it["gold_answer"]],
                         "response": it["gold_answer"],
                         "reference": it["gold_answer"]})
    else:
        client = login(args.base, args.username, args.password)
        kb_id = load_state().get("huatuo_kb_id")
        if not kb_id:
            sys.exit("缺少 huatuo 评测库，请先执行 ingest --source huatuo")
        skipped = 0
        for n, it in enumerate(items, 1):
            question = it["questions"][0]
            dbg, err1 = timed_post_retry(client, "/retrieval/debug", "ragas./retrieval/debug",
                                         json={"kb_id": kb_id, "question": question,
                                               "top_k": args.top_k})
            qa, err2 = timed_post_retry(client, "/qa", "ragas./qa",
                                        json={"kb_id": kb_id, "question": question})
            if dbg is None or qa is None:
                skipped += 1
                print(f"  跳过样本[{n}/{args.limit}] 接口失败 {question[:24]} {err1}{err2}")
                continue
            contexts = [h["content"] for h in (dbg.get("evidence") or []) if h.get("content")]
            rows.append({"user_input": question, "retrieved_contexts": contexts,
                         "response": qa.get("answer") or "",
                         "reference": it["gold_answer"]})
            print(f"  采集样本 [{n}/{args.limit}] {question[:26]}… 上下文 {len(contexts)} 块")
        if skipped:
            print(f"接口重试后仍失败跳过 {skipped} 条")

    llm, embeddings = _ragas_models()
    dataset = EvaluationDataset.from_list(rows)  # 0.4.3：from_list 直接吃 dict 行
    result = evaluate(
        dataset=dataset,
        metrics=[Faithfulness(), ResponseRelevancy(),
                 LLMContextPrecisionWithReference(), LLMContextRecall()],
        llm=llm, embeddings=embeddings,
        run_config=RunConfig(timeout=args.timeout, max_workers=args.concurrency),
    )
    df = result.to_pandas()

    metric_cols = [c for c in ("faithfulness", "answer_relevancy",
                               "context_precision", "llm_context_precision_with_reference",
                               "context_recall") if c in df.columns]
    summary = {"source": args.source, "n": len(rows),
               **{c: round(float(df[c].mean()), 4) for c in metric_cols}}
    print(df[metric_cols].to_string())
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "ragas_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    df.to_json(RESULTS / "ragas_details.json", orient="records", force_ascii=False, indent=2)
    print("明细写入", RESULTS / "ragas_details.json")


# ---------------------------------------------------------------- main

def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base", default=DEFAULT_BASE)
    p.add_argument("--username", default=DEFAULT_USER)
    p.add_argument("--password", default=DEFAULT_PASS)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("ingest")
    s.add_argument("--source", choices=["cmedqa", "huatuo", "all"], default="all")
    s.add_argument("--max-files", type=int, default=0, help="调试用：限制上传分片数")
    s.set_defaults(fn=cmd_ingest)

    s = sub.add_parser("retrieve")
    s.add_argument("--source", choices=["cmedqa", "huatuo"], default="cmedqa")
    s.add_argument("--limit", type=int, default=200)
    s.set_defaults(fn=cmd_retrieve)

    s = sub.add_parser("qa")
    s.add_argument("--limit", type=int, default=30)
    s.add_argument("--no-judge", action="store_true", help="只跑系统回答，不调 judge 模型")
    s.add_argument("--no-cosine", action="store_true", help="跳过答案-金标余弦相似度")
    s.set_defaults(fn=cmd_qa)

    s = sub.add_parser("preference")
    s.add_argument("--limit", type=int, default=50)
    s.set_defaults(fn=cmd_preference)

    s = sub.add_parser("mcq")
    s.add_argument("--limit", type=int, default=100)
    s.add_argument("--kb-source", choices=["cmedqa", "huatuo", "none"], default="huatuo")
    s.set_defaults(fn=cmd_mcq)

    s = sub.add_parser("ragas")
    s.add_argument("--source", choices=["huatuo", "synthetic"], default="huatuo",
                   help="huatuo=真实管线取上下文与回答；synthetic=离线自检（不需要后端）")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--top-k", type=int, default=6, help="检索上下文块数")
    s.add_argument("--timeout", type=int, default=180, help="单指标 LLM 调用超时（秒）")
    s.add_argument("--concurrency", type=int, default=4)
    s.set_defaults(fn=cmd_ragas)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
