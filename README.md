# 传统 NLP 检索增强生成文本问答系统

使用传统 NLP 处理文档，以 MySQL 9 存储业务数据和向量，向量模型为 `text-embedding-v4`，问答模型为 `qwen3.8-flash`。

当前仅完成项目目录及基础配置初始化。Python 文件仅作包标记与入口占位，尚未编写业务代码，服务暂不可运行。未安装依赖、未执行数据库迁移、未连接数据库、未调用或验证向量模型及问答模型。

## 项目结构

```text
NLP_RAG/
├── app/
│   ├── main.py           # 服务入口占位
│   ├── api/              # 知识库 文件 任务 检索 问答接口
│   ├── core/             # 配置 认证 日志 错误处理
│   ├── db/               # 数据模型 数据库会话
│   ├── parsers/          # TXT Markdown DOCX PDF 文档解析
│   ├── nlp/              # 清洗 分句 分词 关键词 规则切块
│   ├── indexing/         # 索引构建 快照发布 缓存装载
│   ├── retrieval/        # BM25 向量召回 RRF 规则重排
│   ├── generation/       # 提示词 上下文 答案与引用校验
│   ├── gateways/         # 向量模型和问答模型适配
│   ├── workers/          # 导入任务 重试 清理
│   └── schemas/          # 请求 响应 内部数据契约
├── migrations/
│   └── versions/         # 数据库迁移脚本预留
├── resources/
│   ├── dictionaries/     # 领域词典
│   └── stopwords/        # 停用词资源
├── tests/
│   ├── fixtures/         # 测试文档及标准问答
│   ├── unit/             # 单元测试预留
│   ├── integration/      # 集成测试预留
│   └── evaluation/       # 检索与答案评测预留
├── data/
│   └── uploads/          # 上传文件目录 不提交真实文件
├── scripts/              # 后续开发和运维脚本
├── docs/                 # 已有 Word 和 Markdown 实现文档
├── .env.example          # 环境变量模板 无真实密钥
├── .gitignore            # 排除密钥 虚拟环境 缓存 上传文件
├── .python-version       # 推荐 Python 3.12
└── pyproject.toml        # 项目元数据 依赖声明 检查工具配置
```

## 基础技术配置

| 项目 | 预设 |
| --- | --- |
| Python | 推荐 3.12，依赖声明支持 3.11 与 3.12 |
| 后端框架 | FastAPI |
| 数据库与向量存储 | MySQL 9 |
| 数据访问与迁移 | SQLAlchemy PyMySQL Alembic |
| 传统 NLP | jieba 规则处理 TF-IDF BM25 |
| 向量模型 | text-embedding-v4，1024 维 |
| 问答模型 | qwen3.8-flash |
| 模型接入 | 百炼 OpenAI 兼容接口 |
| 开发工具 | uv pytest Ruff |

依赖范围仅作初始声明，尚未安装或生成锁定文件。后续开发时可使用 `uv sync --dev` 安装依赖，并将 `.env.example` 复制为 `.env` 后填写实际配置；本次未执行这些操作。

MySQL 社区版默认预设为向量持久化加应用层精确检索，HeatWave 路径保留在配置中。两者的能力区别及后续模块设计见 `docs/` 内的项目实现文档。

## 后续开发顺序

1. 实现配置加载与 FastAPI 入口。
2. 实现传统 NLP 文档解析与处理流程。
3. 编写数据模型、迁移与后台任务。
4. 实现混合检索与模型适配。
5. 实现问答、引用和权限控制。
6. 按需开展数据库、模型联调与评测。
