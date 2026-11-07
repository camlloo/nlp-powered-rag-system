# 传统 NLP 检索增强生成文本问答系统

项目标识与 Gitee 仓库名统一为 `nlp-powered-rag-system`。

使用传统 NLP 处理文档，以 PostgreSQL 16/17 + pgvector 存储业务数据和向量，以 MinIO 存储上传原文件；向量模型为 `text-embedding-v4`，问答模型为 `qwen3.8-flash`。

**数据组织（去知识库化）**：文档不设知识库，直接归属「个人」或「某组织」并设公开/私有——组织公开=所有人可检索；组织私有=上传者与该组织成员；个人公开=所有人；个人私有=仅上传者。问答面向当前用户可见的全部文档（内部由单一主空间承载，见 docs/部署与运维.md §5）。

**当前进度**：开发计划 M0–M8 全部完成——配置/认证/日志/错误、PostgreSQL+pgvector 数据层、四格式解析与传统 NLP 流水线、模型网关（重试/预算/缓存）、快照构建与原子发布、worker 租约、BM25+向量双路检索与 RRF 融合、问答引用与审计、管理接口（列表/删除/清理）、评测 harness 与消融实验、备份恢复演练、传统后台管理界面（Vue3 + vue-router：任务（历史对话）/ 文档管理 / 组织标签 / 用户管理 / 系统设置；组织标签与用户管理接口 `/admin/*`）。249 项测试（单元 + 集成，无数据库自动跳过）。

- **使用与部署**：见 [docs/部署与运维.md](docs/部署与运维.md)（上线步骤、环境变量速查、管理员操作、备份恢复演练、附录 C 检查表核对）。
- **评测**：`tests/evaluation/` 提供评测集格式、指标 harness（Recall@5 / Hit@5 / MRR@10 / 拒答率）与消融实验；种子评测集 26 题用于回归，正式验收须以真实语料扩展至 ≥200 题人工标注（实现文档 §14.1）。

## 快速开始

数据库运行在 WSL docker 容器 `pgvector` 中（镜像 `pgvector/pgvector:pg17`，端口映射 `5433 -> 5432`，账号 `postgres`）。本项目使用独立数据库 `nlp_rag`，pgvector 扩展由首版迁移自动安装。原文件存储使用 MinIO，本机 endpoint 为 `127.0.0.1:9000`，桶为 `nlprag`。

1. 确认 PostgreSQL 容器运行：`wsl docker ps | grep pgvector`；按实际环境准备数据库容器及凭证。
2. 建库（一次性，首次已执行）：`wsl docker exec -u postgres pgvector psql -c "CREATE DATABASE nlp_rag ENCODING 'UTF8' TEMPLATE template0"`
3. 安装依赖：`uv sync`。
4. 配置环境：`cp .env.example .env`；填写 `API_TOKEN`、模型凭证及 MinIO 配置。MinIO 的 `MINIO_ENDPOINT=127.0.0.1:9000`、`MINIO_BUCKET=nlprag`、`MINIO_ACCESS_KEY=<用户名>`、`MINIO_SECURE=false` 为本机配置示例；`MINIO_SECRET_KEY` 通过安全渠道设置，确保桶 `nlprag` 可访问。API 与 worker 必须使用相同的 endpoint、桶、凭证与传输配置，不提交 `.env`。
5. 执行迁移：`uv run alembic upgrade head`。
6. 初始化种子数据：`uv run python -m scripts.seed`。
7. 数据库能力验证（PG 版本、pgvector 版本、向量读写往返）：`uv run python -m scripts.verify_db`。
8. 启动服务：`uv run uvicorn app.main:app --reload`。
   - 接口文档 http://127.0.0.1:8000/docs
   - 健康检查 `GET /healthz`（存活）、`GET /readyz`（就绪）；探针通过不代表 worker 或付费模型可用，仍需单独验证 MinIO 原文件读写，详见部署手册。
9. 启动前端（可选，开发模式）：`cd frontend && npm install && npm run dev`，访问 http://127.0.0.1:5173；
   生产模式 `npm run build` 后由后端同源托管 `frontend/dist`，直接访问 http://127.0.0.1:8000。
10. 常驻 worker（上传文档/重建索引必需）：`uv run python -m app.workers`。

注：若 `127.0.0.1:5433` 连不上，用 `wsl hostname -I` 获取 WSL IP 填入 `.env` 的 `PG_HOST`。`127.0.0.1:9000` 仅适用于 API 和 worker 都能通过各自的回环地址访问同一 MinIO 的环境；跨容器或跨主机部署时应改成双方均可访问的地址。

**原文件与切换风险**：对象键采用 `YYYY/MM/DD/用户ID/文件名去扩展名/原始文件名`，例如 `2026/11/07/123/报告/报告.pdf`。同一对象键已存在时先删除再上传；若上传失败，原对象可能无法恢复，操作前应确保可恢复备份。旧本地文件及对应数据库记录选择彻底删除、不迁移；应先停止写入、核对范围并制定备份和恢复方案，再由运维人员执行，详见部署手册。

**认证说明**：首期使用静态 API Token（环境变量 `API_TOKEN`），请求头 `X-API-Token: <token>` 或 `Authorization: Bearer <token>`。tenant_id/user_id 从服务端凭证解析，不信任请求体。

**测试**：`uv run pytest`（未启动数据库时集成测试自动跳过）。

## 项目结构

```text
nlp-powered-rag-system/
├── app/
│   ├── main.py           # FastAPI 入口与健康检查
│   ├── api/              # 路由与依赖项：auth documents 知识库(内部) 任务 检索 问答 admin（管理面）
│   ├── core/             # 配置 认证 日志 错误处理
│   ├── db/               # ORM 模型 数据库会话
│   ├── parsers/          # TXT Markdown DOCX PDF 文档解析
│   ├── nlp/              # 清洗 分句 分词 关键词 规则切块
│   ├── indexing/         # 索引构建 快照发布 缓存装载
│   ├── retrieval/        # BM25 向量召回 RRF 规则重排
│   ├── generation/       # 提示词 上下文 答案与引用校验
│   ├── gateways/         # 向量模型和问答模型适配
│   ├── workers/          # 导入任务 重试 清理
│   └── schemas/          # 请求 响应 内部数据契约
├── frontend/             # Vue3 + Vite 传统后台（对话检索/文档管理/组织标签/用户管理/系统设置）
├── migrations/           # Alembic 迁移（0001 建表基线）
├── resources/
│   ├── dictionaries/     # 领域词典
│   └── stopwords/        # 停用词资源
├── tests/
│   ├── fixtures/         # 测试文档及标准问答
│   ├── unit/             # 单元测试
│   ├── integration/      # 集成测试（无数据库自动跳过）
│   └── evaluation/       # 检索与答案评测预留
├── scripts/
│   ├── seed.py           # 种子数据
│   └── verify_db.py      # 数据库能力与向量读写验证
├── docs/                 # 实现文档（V1.1）与开发计划
├── .env.example          # 环境变量模板 无真实密钥
├── .python-version       # 推荐 Python 3.12
└── pyproject.toml        # 项目元数据 依赖声明 检查工具配置
```

## 基础技术配置

| 项目 | 预设 |
| --- | --- |
| Python | 推荐 3.12，依赖声明支持 3.11 与 3.12 |
| 后端框架 | FastAPI |
| 数据库与向量存储 | PostgreSQL 16/17 + pgvector |
| 原文件存储 | MinIO（本机 `127.0.0.1:9000`，桶 `nlprag`） |
| 数据访问与迁移 | SQLAlchemy 2、psycopg 3、pgvector Python 绑定、Alembic |
| 传统 NLP | jieba 规则处理 TF-IDF BM25 |
| 向量模型 | text-embedding-v4，1024 维 |
| 问答模型 | qwen3.8-flash |
| 模型接入 | 百炼 OpenAI 兼容接口 |
| 前端 | Vue 3 + Vite 传统后台管理界面（任务/历史对话 · 文档管理 · 组织标签 · 用户管理 · 系统设置） |
| 开发工具 | uv pytest Ruff |

依赖在集成测试通过后锁定完整版本。设计约束与模块说明见 `docs/` 内的实现文档（V1.1）与开发计划。

## 后续开发顺序

按 `docs/开发计划.md` 的里程碑推进：M2 解析与 NLP 流水线 → M3 模型网关 → M4 索引与任务 → M5 检索 → M6 问答与引用 → M7 管理接口 → M8 评测与交付。
