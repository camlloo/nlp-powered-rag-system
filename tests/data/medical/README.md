# 医疗问答测试集

中文为主的医疗问答评测数据，由 `tests/build_medical_evalset.py` 从
`data/medical/raw/` 原始数据构建；跑评测用 `tests/eval_medical.py`。

## 目录结构

```
qa_open_test.jsonl / qa_open_dev.jsonl   开放问答（华佗百科，1000+1000 条）
  {id, questions: [同义改写...], gold_answer, source}
mcq_zh.jsonl                              中文医学选择题（CMMLU 8 个子集，1646 条）
  {id, subject, question, options: {A..D}, answer, source}
retrieval/queries.jsonl                   检索查询（cMedQA，3999 条）{id, text}
retrieval/qrels.jsonl                     相关性标注（7449 条）{qid, pid, score}
retrieval/corpus_eval.jsonl               评测语料（10000 篇 = 全部金标 + 随机干扰）{pid, text}
kb/cmedqa/cmedqa-part-*.md                上传建库用语料分片（40 个）
kb/huatuo/huatuo-part-*.md                百科答案知识库分片（10 个，端到端 QA 用）
                                          （文件名必须带源前缀：对象 key 以日期+用户+文件名
                                          为粒度，跨库同名同日上传会被 409 拒绝）
manifest.json                             构建统计
results/                                  评测输出（自动创建）
```

## 数据来源与获取（国内走 hf-mirror.com，直连 huggingface.co 超时）

| 数据集 | 用途 | 原始文件 |
|---|---|---|
| FreedomIntelligence/huatuo_encyclopedia_qa | 开放问答 gold + 华佗知识库 | raw/huatuo_enc_test.jsonl, huatuo_enc_val.jsonl |
| C-MTEB/CmedqaRetrieval + qrels | 检索 recall/MRR 评测 + cMedQA 知识库 | raw/cmedqa_corpus/queries/qrels.parquet |
| lmlmcat/cmmlu（cmmlu_v1_0_1.zip） | 医学选择题（解剖/临床知识/中医等） | raw/cmmlu/test/*.csv |

重新下载示例：

```bash
curl -sL -o data/medical/raw/huatuo_enc_test.jsonl \
  https://hf-mirror.com/datasets/FreedomIntelligence/huatuo_encyclopedia_qa/resolve/main/test_datasets.jsonl
curl -sL -o data/medical/raw/cmedqa_corpus.parquet \
  https://hf-mirror.com/datasets/C-MTEB/CmedqaRetrieval/resolve/main/data/corpus-00000-of-00001-a3949861f65a3226.parquet
```

重建：`uv run --with pyarrow python tests/build_medical_evalset.py`

## 评测（后端启动后）

```bash
uv run python tests/eval_medical.py ingest --source all          # 建两个评测库并灌语料
uv run python tests/eval_medical.py retrieve --source cmedqa     # recall@1/5/10, MRR@10
uv run python tests/eval_medical.py retrieve --source huatuo
uv run python tests/eval_medical.py qa --limit 30                # /qa + LLM judge：二元分+五维0-5分+金标余弦
uv run python tests/eval_medical.py mcq --limit 100              # 选择题准确率
uv run python tests/eval_medical.py ragas --limit 20             # RAGAS 四指标（真实管线）
uv run python tests/eval_medical.py ragas --source synthetic --limit 3  # RAGAS 离线自检
uv run python tests/eval_medical.py preference --limit 50        # RAG vs 裸模型盲评胜出率（借鉴论文）
```

### 论文借鉴项（王永刚《基于检索增强与大模型的医疗问答方法研究》）

| 借鉴点 | 落点 | 输出 |
|---|---|---|
| 0–5 五维裁判评分（准确性/完整性/逻辑性/专业性/安全性，论文 §5.6.3） | `qa` | `results/qa_summary.json`（dim_means_0_5）+ `qa_open.jsonl` 逐条 dims |
| 盲评胜出率（论文表 4-4~4-6） | `preference`（RAG 全管线 vs 同底座裸模型直答，随机位次） | `results/preference_summary.json` 分维 win/tie 百分比 |
| 性能效率指标（成功率/平均·最小·最大响应时间，论文表 5-2） | 各命令逐请求计时 | `results/system_perf.json` 分节合并 |
| 答案-金标余弦相似度（论文 §3.3.2） | `qa`（百炼 embedding） | `qa_summary.json.cosine_gold_mean` |

不采纳 BERTScore：需引入 torch、医疗长答案上组间区分度差（论文各组 F1 差距 <0.02），由 RAGAS 四指标与盲评覆盖。
裁判沿用项目百炼 chat_model（与生成底座同族，同族偏差在评测报告中披露）。

### RAGAS 指标化评测

框架 ragas 0.4.3（dev 依赖），裁判 LLM 与 embedding 复用项目 `.env` 的百炼配置。
对 `qa_open_test.jsonl` 抽样，逐题走 `/retrieval/debug`（取 evidence 作
retrieved_contexts）+ `/qa`（取 response），reference=金标答案，计算：

| 指标 | 含义 |
|---|---|
| faithfulness | 回答是否忠于检索到的上下文（防幻觉） |
| answer_relevancy | 回答与问题的语义相关度（embedding 余弦） |
| llm_context_precision_with_reference | 检索上下文相对参考答案的精确率 |
| context_recall | 上下文对参考答案要点的覆盖率 |

结果写入 `results/ragas_summary.json` + `results/ragas_details.json`。
两处环境适配（已在脚本内处理）：
1. ragas 0.4.3 对 langchain-community 0.4（已移除 vertexai 模块）的导入用 stub 兼容；
2. 裁判调用带 `enable_thinking:false`——百炼思考模式既拖慢评测，又与
   answer_relevancy 的 n>1 采样请求冲突（400）。

`--source synthetic` 自检模式不需要后端：金标答案同时充当上下文与回答，
四项指标应接近满分，用于快速验证模型配置与评测链路。

抽样固定种子 42，结果可复现；`--limit` 可加大覆盖。默认评测账号
`med_eval / MedEval!2026`（不存在会自动注册），可用 `--username/--password` 覆盖。

## 判定口径说明

- 检索命中 = 最终证据列表（evidence→reranked→fused 依次取用）中某 chunk 文本
  与金标文档文本互相包含（归一化去空白/标点后比较）；
- 开放问答 = 裁判模型对照参考答案给二元分 1 / 0.5 / 0，并附五维 0–5 分与拒答理由；
- 盲评胜出率 = 裁判在两个匿名候选（RAG/裸模型）中逐维选优，允许 tie，随机左右位次；
- 选择题 = 正则从系统回答抽 A-D 字母（优先“答案是X”句式）。
