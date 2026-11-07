# 传统 NLP 检索增强生成文本问答系统

项目标识与 Gitee 仓库名统一为 `nlp-powered-rag-system`。

使用传统 NLP 处理文档，以 PostgreSQL 16/17 + pgvector 统一存储业务数据和向量，向量模型为 `text-embedding-v4`，问答模型为 `qwen3.8-flash`。

**当前进度**：开发计划 M0–M8 全部完成——配置/认证/日志/错误、PostgreSQL+pgvector 数据层、四格式解析与传统 NLP 流水线、模型网关（重试/预算/缓存）、快照构建与原子发布、worker 租约、BM25+向量双路检索与 RRF 融合、问答引用与审计、管理接口（列表/删除/清理）、评测 harness 与消融实验、备份恢复演练。206+ 项测试（单元 + 集成，无数据库自动跳过）。

- **使用与部署**：见 [docs/部署与运维.md](docs/部署与运维.md)（上线步骤、环境变量速查、管理员操作、备份恢复演练、附录 C 检查表核对）。
- **评测**：`tests/evaluation/` 提供评测集格式、指标 harness（Recall@5 / Hit@5 / MRR@10 / 拒答率）与消融实验；种子评测集 26 题用于回归，正式验收须以真实语料扩展至 ≥200 题人工标注（实现文档 §14.1）。

## 快速开始

数据库运行在 WSL docker 容器 `pgvector` 中（镜像 `pgvector/pgvector:pg17`，端口映射 `5433 -> 5432`，账号 `postgres`）。本项目使用独立数据库 `nlp_rag`，pgvector 扩展由首版迁移自动安装。

1. 确认容器运行：`wsl docker ps | grep pgvector`
   （如需重建：`wsl docker run -d --name pgvector -e POSTGRES_PASSWORD=postgres -p 5433:5432 pgvector/pgvector:pg17`）
2. 建库（一次性，首次已执行）：`wsl docker exec -u postgres pgvector psql -c "CREATE DATABASE nlp_rag ENCODING 'UTF8' TEMPLATE template0"`
3. 安装依赖：`uv sync`
4. 配置环境：`cp .env.example .env`（本机默认值已对齐上述容器，`API_TOKEN` 建议修改）
5. 执行迁移：`uv run alembic upgrade head`
6. 初始化种子数据：`uv run python -m scripts.seed`
7. 数据库能力验证（PG 版本、pgvector 版本、向量读写往返）：`uv run python -m scripts.verify_db`
8. 启动服务：`uv run uvicorn app.main:app --reload`
   - 接口文档 http://127.0.0.1:8000/docs
   - 健康检查 `GET /healthz`（存活）、`GET /readyz`（就绪，检查数据库与 pgvector 扩展）

注：若 `127.0.0.1:5433` 连不上，用 `wsl hostname -I` 获取 WSL IP 填入 `.env` 的 `PG_HOST`。

**认证说明**：首期使用静态 API Token（环境变量 `API_TOKEN`），请求头 `X-API-Token: <token>` 或 `Authorization: Bearer <token>`。tenant_id/user_id 从服务端凭证解析，不信任请求体。

**测试**：`uv run pytest`（未启动数据库时集成测试自动跳过）。

## 项目结构

```text
nlp-powered-rag-system/
├── app/
│   ├── main.py           # FastAPI 入口与健康检查
│   ├── api/              # 路由、依赖项（认证/授权校验点预留）知识库 文件 任务 检索 问答接口
│   ├── core/             # 配置 认证 日志 错误处理
│   ├── db/               # ORM 模型 数据库会话
│   ├── parsers/          # TXT Markdown DOCX PDF 文档解析（待实现）
│   ├── nlp/              # 清洗 分句 分词 关键词 规则切块（待实现）
│   ├── indexing/         # 索引构建 快照发布 缓存装载（待实现）
│   ├── retrieval/        # BM25 向量召回 RRF 规则重排（待实现）
│   ├── generation/       # 提示词 上下文 答案与引用校验（待实现）
│   ├── gateways/         # 向量模型和问答模型适配（待实现）
│   ├── workers/          # 导入任务 重试 清理（待实现）
│   └── schemas/          # 请求 响应 内部数据契约（待实现）
├── migrations/           # Alembic 迁移（0001 建表基线）
├── resources/
│   ├── dictionaries/     # 领域词典
│   └── stopwords/        # 停用词资源
├── tests/
│   ├── fixtures/         # 测试文档及标准问答
│   ├── unit/             # 单元测试
│   ├── integration/      # 集成测试（无数据库自动跳过）
│   └── evaluation/       # 检索与答案评测预留
├── data/
│   └── uploads/          # 上传文件目录 不提交真实文件
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
| 数据访问与迁移 | SQLAlchemy 2、psycopg 3、pgvector Python 绑定、Alembic |
| 传统 NLP | jieba 规则处理 TF-IDF BM25 |
| 向量模型 | text-embedding-v4，1024 维 |
| 问答模型 | qwen3.8-flash |
| 模型接入 | 百炼 OpenAI 兼容接口 |
| 前端 | Vue 3（后期实现），首期以 OpenAPI 文档调试 |
| 开发工具 | uv pytest Ruff |

依赖在集成测试通过后锁定完整版本。设计约束与模块说明见 `docs/` 内的实现文档（V1.1）与开发计划。

## 后续开发顺序

按 `docs/开发计划.md` 的里程碑推进：M2 解析与 NLP 流水线 → M3 模型网关 → M4 索引与任务 → M5 检索 → M6 问答与引用 → M7 管理接口 → M8 评测与交付。
