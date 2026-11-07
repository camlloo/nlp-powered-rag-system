# 传统 NLP 检索增强生成文本问答系统

面向团队/个人的私有文档问答系统：用传统 NLP（jieba 分词、TF-IDF/BM25）与向量语义双路召回，配合大模型在**可核验的文档证据**上生成带引用的回答，并做检索级权限隔离与全链路审计。

- 项目标识与 Gitee 仓库名统一为 `nlp-powered-rag-system`。
- 业务数据与向量存于 PostgreSQL + pgvector，上传原文件存于 MinIO。
- 向量模型 `text-embedding-v4`（1024 维），问答模型 `qwen3.8-flash`（百炼 OpenAI 兼容接口）。

## 功能特性

- **问答与引用**：双路召回（BM25 + 向量独立全库召回）→ RRF 融合 → 规则重排 → 保守证据选择；答案携带 `[S1]` 式引注章，点击定位引用原文；证据不足时拒答。
- **检索级权限**：文档归属「个人 / 某组织」× 可见性「公开 / 私有」，问答只在该用户可见范围内检索（无需知识库概念）。
- **任务模块**：历史对话持久化，新建任务 / 继续追问 / 删除，侧边栏任务列表；对话从问答审计原样还原。
- **管理后台**：文档管理（上传、归属与可见性修改、删除、全量重建）、组织标签管理、用户管理（授予/撤销管理员、打标）、系统设置；前端为手绘涂鸦风传统后台。
- **可靠工程**：快照原子发布 + 修订号复核、worker 租约与过期恢复、软删除 + 物理清理、幂等上传、请求级 `request_id` 关联的 JSON 日志与统一错误响应、备份恢复演练与评测 harness。

## 技术栈

| 层面 | 选型 |
| --- | --- |
| 语言 / 运行时 | Python 3.11–3.12（推荐 3.12）；Node.js（前端） |
| 后端框架 | FastAPI + Uvicorn |
| 数据与向量 | PostgreSQL 16/17 + pgvector；SQLAlchemy 2、psycopg 3、Alembic |
| 原文件对象存储 | MinIO（S3 兼容） |
| 传统 NLP | jieba 分词、规则清洗/分句/关键词、TF-IDF / BM25 |
| 模型接入 | 百炼 OpenAI 兼容接口（embedding `text-embedding-v4`、chat `qwen3.8-flash`） |
| 前端 | Vue 3 + Vite + vue-router（同源由后端托管构建产物） |
| 工具链 | uv、pytest、Ruff |

## 架构概览

```
上传(MinIO 对象 + DB 记录/任务) → worker：解析 → 传统 NLP 流水线 → 切块 → 向量化
     → 快照原子发布(data_revision) → 运行时索引缓存
提问 → 查询预处理 → BM25 与向量双路召回 → RRF 融合 → 规则重排 → 证据门槛/拒答
     → 上下文构建(带引用预算) → 生成 → 引用校验 → 审计(qa_request/qa_citation/qa_feedback)
```

数据按 `tenant_id` 隔离；权限由文档级 `uploader_user_id / visibility / org_tag_id` 承载，检索与引用读取时逐条复核。内部以单一「主空间」挂载快照与任务，知识库不再作为产品概念暴露。

## 前置要求

- WSL + Docker：运行 `pgvector/pgvector:pg17` 容器（端口 `5433 -> 5432`）与 MinIO 服务（本机 `127.0.0.1:9000`）。
- `uv`（管理 Python 依赖）、`node`/`npm`（前端构建，可选）。
- 百炼模型凭证：`MODEL_BASE_URL` 与 `DASHSCOPE_API_KEY`（缺失时问答直接报 `model_auth_error`，不静默回落 Mock）。

## 快速开始

在 WSL Docker 中准备数据库容器（首次）：

```bash
wsl docker run -d --name pgvector -e POSTGRES_PASSWORD=postgres -p 5433:5432 pgvector/pgvector:pg17
wsl docker exec -u postgres pgvector psql -c "CREATE DATABASE nlp_rag ENCODING 'UTF8' TEMPLATE template0"
```

安装依赖并配置：

```bash
uv sync
cp .env.example .env       # 填写 API_TOKEN、模型凭证与 MinIO 配置（不提交 .env）
```

`.env` 关键项（完整分组见 [部署与运维手册](docs/部署与运维.md)）：

```dotenv
API_TOKEN=change-me
MODEL_BASE_URL=<百炼兼容接口地址>
DASHSCOPE_API_KEY=<密钥>
MINIO_ENDPOINT=127.0.0.1:9000
MINIO_BUCKET=nlprag
MINIO_ACCESS_KEY=<用户名>
MINIO_SECRET_KEY=<安全渠道注入>
MINIO_SECURE=false
```

迁移、初始化并启动：

```bash
uv run alembic upgrade head          # pgvector 扩展与建表由迁移安装
uv run python -m scripts.seed        # 确保主空间存在（幂等）
uv run uvicorn app.main:app --reload # API，http://127.0.0.1:8000，文档 /docs
uv run python -m app.workers         # 常驻 worker（上传/重建必需，另开终端）
```

访问 **http://127.0.0.1:8000**（生产模式下由后端同源托管前端构建产物）。健康检查：`GET /healthz`（存活）、`GET /readyz`（就绪，仅校验数据库与 pgvector，不校验 MinIO/worker）。

前端开发模式（可选，热更新，API 代理到 :8000）：

```bash
cd frontend && npm install && npm run dev   # http://127.0.0.1:5173
```

## 认证与安全

- **用户体系**：`POST /auth/register`、`POST /auth/login` 签发 JWT（HS256），口令 scrypt 哈希。
- **管理员引导**：登录页「管理员令牌」输入 `.env` 的 `API_TOKEN`（请求头 `X-API-Token` 或 `Authorization: Bearer`）进入后台，在「用户管理」将真实用户提升为 `is_admin`；系统始终保留最后一名管理员。
- 凭证仅从服务端解析，不信任请求体；日志与错误响应不记录正文、密钥或内部路径。

## 数据组织与存储

- **归属 × 可见性**：组织公开 = 所有人可检索；组织私有 = 上传者与该组织成员；个人公开 = 所有人；个人私有 = 仅上传者。删除组织标签会使该组织文档退回上传者个人归属并即时生效。
- **原文件对象键**：`年/月/日/用户ID/文件名去扩展名/原始文件名`（不改名），例如 `2026/11/08/123/报告/报告.pdf`；数据库仅存不含凭据的 `s3://<bucket>/<编码key>` 与内容 SHA-256。
- **同名先删后传**：上传经持久写入屏障串行，删除旧对象后若上传失败原文件可能不可恢复——重要文件请事先独立备份。旧本地存储及记录已在迁移时彻底清除，详见部署手册。

## API 概览

| 领域 | 端点 | 说明 |
| --- | --- | --- |
| 认证 | `POST /auth/register` · `POST /auth/login` · `GET /auth/me` | 注册 / 登录 / 当前用户 |
| 文档 | `POST /documents` · `GET /documents` · `PATCH /documents/{id}` · `DELETE /documents/{id}` | 上传（scope+visibility）/ 全局清单 / 改归属与可见性 / 删除 |
| 问答 | `POST /qa` · `POST /qa/stream`(SSE) · `GET /citations/{id}` · `POST /feedback` | 问答（kb_id 缺省=主空间）/ 引用详情 / 反馈 |
| 检索调试 | `POST /retrieval/debug` | 双路召回/融合/重排/门槛明细 |
| 任务 | `GET /jobs/{id}` | 处理状态（status+stage+error） |
| 会话 | `GET/POST /conversations` · `GET/DELETE /conversations/{id}` | 任务列表 / 新建 / 历史还原 / 删除 |
| 管理面 | `/admin/org-tags*` · `/admin/users*` · `POST /admin/reindex` | 组织标签 / 用户 / 全量重建（管理员） |
| 内部兼容 | `/kbs*` | 快照索引以 kb_id 为键的内部实现，前端不使用 |

接口文档以运行实例的 `/docs`（OpenAPI）为准。

## 测试

```bash
uv run pytest                 # 离线单元 + 隔离数据库集成/评测；未提供隔离库时集成自动跳过
node --test frontend/tests/api.test.js   # 前端 API 客户端测试
```

- 集成/评测测试使用**显式随机空白隔离库**（`NLP_RAG_TEST_DATABASE_URL`）与内存 fake MinIO，绝不连接共享开发库或正式桶。
- 当前规模：424 项测试通过（静态检查 Ruff 全绿）。
- 评测 harness 与消融实验见 `tests/evaluation/`（指标 Recall@5 / Hit@5 / MRR@10 / 拒答率），种子评测集 26 题用于回归。

## 项目结构

```text
nlp-powered-rag-system/
├── app/
│   ├── main.py            # FastAPI 入口、中间件与健康检查
│   ├── api/               # 路由与授权：auth/documents/conversations/jobs/qa/retrieval/admin/kbs
│   ├── core/              # 配置、认证、日志、错误
│   ├── db/                # ORM 模型、会话、主空间、上传屏障
│   ├── storage.py         # MinIO 原文件存储（禁用重试、单分片 PUT）
│   ├── parsers/           # TXT / Markdown / DOCX / PDF 解析与三重校验
│   ├── nlp/               # 清洗、分句、分词、关键词、规则切块
│   ├── indexing/          # 快照构建、原子发布、缓存装载、物理清理
│   ├── retrieval/         # BM25 与向量召回、RRF 融合、规则重排
│   ├── generation/        # 提示词、上下文、答案与引用校验、问答审计
│   ├── gateways/          # 向量/问答模型适配（重试、预算、缓存）
│   └── workers/           # 租约领取、心跳、过期恢复、清理调度
├── frontend/              # Vue3 + Vite 传统后台（任务/文档管理/组织标签/用户管理/系统设置）
├── migrations/            # Alembic 迁移（0001 建表基线 … 0006 上传屏障）
├── resources/             # 领域词典与停用词
├── tests/                 # unit / integration / evaluation / fixtures / support(fake 存储)
├── scripts/               # seed · verify_db · backup_drill · purge_legacy_uploads
├── docs/                  # 部署与运维手册
└── pyproject.toml         # 依赖与工具配置（uv / pytest / Ruff）
```

## 运维与部署

完整上线步骤、环境变量分组速查、管理员操作、备份恢复演练（pg_dump 往返 + 向量逐块比对）与检查表见 **[docs/部署与运维.md](docs/部署与运维.md)**。要点：数据库与 MinIO 需分别备份并验证一致恢复点；worker 不自动重排失败任务，生产需外部定时补投。

## 里程碑

M0–M8 已全部完成：配置/认证/日志/错误 → 数据层（PostgreSQL+pgvector）→ 四格式解析与传统 NLP 流水线 → 模型网关 → 快照与原子发布 → worker 租约 → 双路检索与 RRF → 问答引用与审计 → 管理接口 → 评测与交付；随后完成管理后台/任务模块改造、去知识库化（组织/个人归属）与 MinIO 原文件存储迁移。演进方向：多租户与 kb_member RBAC 深化、对象存储版本化与孤儿对象巡检、检索阈值以更大真实语料校准。

## 许可证

本仓库当前未声明开源许可证；如需对外开源请先补充 `LICENSE`。
