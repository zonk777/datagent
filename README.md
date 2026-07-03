# DataAgent 数据智能体服务系统

50组根据《24项目库文档@数据智能体服务系统》开发的企业级数据智能体系统，基于 FastAPI + Vue 3 架构，支持自然语言数据分析、ReAct Agent 工具调用、业务知识库 RAG 问答、多轮连续追问、文件文档智能分析、多维度分析报告生成与导出。

## 核心能力

### 智能分析引擎
- **ReAct Agent 工具调用**：Reasoning-Action-Observation 循环（最大 5 轮），LLM 自主选择 query_data / search_knowledge_base / compare_periods / python_analyze / finish_analysis 等工具完成分析
- **MCP 协议集成**：支持 Model Context Protocol（JSON-RPC over HTTP），动态发现和调用外部工具，多 MCP Server 聚合与降级
- **四阶段分析流水线**：意图分类与 Persona 匹配 → 多维度分析规划 → 分段执行与 SSE 流式推送 → 结果校验与报告生成
- **Persona 自适应框架**：内置数据分析师、财务审计师等多种角色 YAML 配置，不同角色自动切换分析框架、图表偏好和输出风格

### 数据接入与管理
- **双后端数据库**：支持 SQLite（开箱即用）和 MySQL（生产环境），连接池管理（最大 10 连接），`?`/`%s` 占位符自动适配
- **MySQL 高级连接**：SSL/TLS 加密连接、SSH 堡垒机隧道、读写超时配置
- **文件上传**：CSV / Excel 上传自动识别字段与数据类型，分片上传支持大文件断点续传（阈值 8MB，单分片 5MB）
- **元数据管理**：字段名、数据类型、描述、样例值、空值率自动采集，支持字段别名与灵活映射

### 业务知识库
- **RAG 检索增强**：LLM 查询改写 → 三路并行召回（语义向量 / 关键词 / 标题匹配）→ LLM 重排序
- **Qdrant Server 向量存储**：远程 HTTP 向量数据库（`172.20.10.3:6333`），替代本地文件模式
- **语义缓存**：Embedding 余弦相似度匹配（阈值 0.95），200 条 SQLite 缓存，命中即返回无需重复调用 LLM
- **文档导入**：支持 PDF / Word / Markdown / TXT 上传，自动分段并存入知识库

### 文档智能分析
- **聊天框文件分析**：在智能分析对话框中直接上传 PDF / Word / MD / TXT 文件，无需依赖数据源即可分析
- **双路径路由**：纯文件分析走专用 `document_analyzer` 引擎；用户明确要求结合数据库时才走 ReAct + SQL 路径
- **全文直读与分段提炼**：短文档（≤12000 字）全文直读 LLM 分析，长文档自动分段提炼（每段 5500 字，最大 10 段）后综合

### 报告生成
- **多图表分析报告**：每个分析维度独立图表，支持柱状图 / 折线图 / 饼图 / 散点图 / 面积图 / 雷达图
- **多格式导出**：HTML（在线预览）/ DOCX（Word）/ PDF（多章节含封面、摘要、附录）/ Markdown
- **图表自定义**：导出时可切换每个章节的图表类型

### 安全与治理
- **只读 SQL 安全校验**：仅允许单条 SELECT/WITH 查询，禁止 INSERT/UPDATE/DELETE/DROP/ALTER 等写入语句，校验授权表名与 CTE 引用
- **RBAC 权限控制**：初始管理员 / 普通管理员角色，数据集级权限分配，支持列级掩码
- **审计日志**：全量操作记录（分析请求、导出报告、删除会话等），支持筛选与 Excel 导出
- **速率限制**：内存 60s 滑动窗口，登录 5 次/分、分析 30 次/分、上传 10 次/分
- **密钥安全**：前端不接触任何 API Key，`error_messages.py` 自动脱敏日志中的 sk- 前缀密钥和 Bearer Token

### 对话体验
- **多轮连续追问**：LLM 语义合并追问上下文，压缩历史摘要注入（最近 3 条原文 + 中段 LLM 摘要 + 超出丢弃）
- **跨会话用户画像**：自动学习用户偏好指标、常用维度、历史话题，跨会话注入分析上下文
- **SSE 流式推送**：分析过程实时展示思考步骤、工具调用、SQL 执行和洞察生成
- **历史会话管理**：会话列表、恢复、删除，消息持久化

## 技术栈

| 层级 | 技术 |
|------|------|
| 后端框架 | FastAPI (Python 3.11+) |
| 前端框架 | Vue 3 + TypeScript + Vite |
| 图表 | ECharts 5 |
| 数据库 | SQLite / MySQL 8.0（双后端自适应） |
| 向量库 | Qdrant Server（远程 HTTP） |
| 文档解析 | python-docx / pypdf / openpyxl / xlrd |
| 报告生成 | ReportLab (PDF) / python-docx (Word) |
| LLM 网关 | OpenAI 兼容接口（DeepSeek / 硅基流动） |
| 容器化 | Docker Compose（app + MySQL + Qdrant） |

## 项目结构

```text
backend/
├── app/
│   ├── main.py                  FastAPI 应用入口、中间件、生命周期
│   ├── config.py                配置管理（Pydantic Settings，读取 .env）
│   ├── db.py                    数据库连接池、双后端路由、Schema 初始化
│   ├── models.py                Pydantic 请求/响应模型
│   ├── routers/                 API 路由层
│   │   ├── agent.py             智能分析、ReAct、报告流、文件分析、SSE
│   │   ├── auth.py              登录、登出、管理员 CRUD
│   │   ├── datasets.py          数据源上传、导入、预览、质量、删除
│   │   ├── knowledge.py         知识库 CRUD、文档导入、重索引
│   │   ├── audit.py             审计日志查询与导出
│   │   ├── mcp.py               MCP JSON-RPC 端点
│   │   └── system.py            健康检查、配置状态、API 设置
│   ├── services/                业务逻辑层
│   │   ├── analyzer.py           分析编排器（5 条分析路径）
│   │   ├── react_agent.py        ReAct Agent 循环与工具调度
│   │   ├── tool_registry.py      7 个 Function Calling 工具定义与执行
│   │   ├── document_analyzer.py  文件文档专用分析引擎
│   │   ├── analysis_planner.py   多维度分析计划生成
│   │   ├── dimension_executor.py 分维度执行器（SQL→数据→图表→叙事）
│   │   ├── meta_router.py        意图路由 + Persona 匹配
│   │   ├── intent_classifier.py  意图分类器（规则 + LLM 双路径）
│   │   ├── sql_generator.py      LLM SQL 生成与自动修复（最大 3 次）
│   │   ├── python_executor.py    Python/Pandas 深度分析沙箱
│   │   ├── field_aliases.py      字段别名灵活映射
│   │   ├── knowledge.py          RAG 检索（查询改写→并行召回→重排序）
│   │   ├── knowledge_documents.py 文档解析、分段、导入
│   │   ├── memory.py             对话记忆压缩与用户画像
│   │   ├── semantic_cache.py     语义缓存（Embedding 余弦相似度）
│   │   ├── reports.py            多格式报告生成（HTML/Word/PDF/MD）
│   │   ├── chart_recommender.py  图表类型推荐
│   │   ├── data_profiler.py      数据集质量分析
│   │   ├── security.py           SQL 只读安全校验
│   │   ├── error_messages.py     错误消息格式化与脱敏
│   │   ├── mcp_server.py / mcp_client.py  MCP 协议实现
│   │   ├── permissions.py        数据集权限校验
│   │   ├── audit.py              审计日志写入
│   │   └── auth.py               密码哈希、会话管理
│   └── personas/                 Persona 角色配置
│       ├── data_analyst.yaml     数据分析师
│       └── financial_auditor.yaml 财务审计师
├── storage/                      本地持久化（SQLite 数据库、Qdrant 向量）
├── requirements.txt
└── .env                          环境配置（不提交 Git）

frontend/
├── src/
│   ├── App.vue                   主应用组件
│   ├── api.ts                    API 客户端（fetch/SSE/XHR 上传）
│   ├── types.ts                  TypeScript 类型定义
│   ├── views/
│   │   ├── OverviewView.vue      工作台
│   │   ├── AnalystView.vue       智能分析（对话框、文件上传、结果展示）
│   │   ├── DatasetsView.vue      数据源管理
│   │   ├── KnowledgeView.vue     业务知识库
│   │   ├── AccountsView.vue      账户管理
│   │   ├── SettingsView.vue      系统配置
│   │   └── AuditView.vue         审计日志
│   └── components/
│       ├── ResultChart.vue       图表展示（ECharts）
│       ├── ThinkingBlock.vue     思考过程折叠面板
│       ├── TypewriterText.vue    打字机效果文本
│       └── AppIcon.vue           图标组件
└── vite.config.ts                Vite 配置（代理到后端 :8000）

docs/                             项目文档
scripts/                          CMD / PowerShell 一键启动脚本
review-reports/                   评审报告（需求跟踪、覆盖度、代码质量等）
docker-compose.yml                Docker 部署编排
```

## 快速开始

### 环境要求

- Python 3.11+
- Node.js 18+
- 现代浏览器（Edge / Chrome）

### 一键启动

CMD：
```cmd
scripts\start-dev.cmd
```

PowerShell：
```powershell
.\scripts\start-dev.ps1
```

脚本自动启动后端（`:8000`）和前端（`:5173`）并打开浏览器。

### 手动启动

**后端**：
```cmd
cd backend
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

**前端**（另开窗口）：
```cmd
cd frontend
npm install
npm run dev
```

- 前端：[http://localhost:5173](http://localhost:5173)
- API 文档：[http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)

### Docker 部署

```cmd
docker-compose up -d
```

启动 app + MySQL 8.0 + Qdrant 三个服务。

## 配置说明

编辑 `backend/.env`：

```dotenv
# 数据库后端选择
DATABASE_BACKEND=mysql          # sqlite（默认）或 mysql

# MySQL 连接（DATABASE_BACKEND=mysql 时生效）
MYSQL_HOST=172.20.10.3
MYSQL_PORT=3306
MYSQL_USER=dataagent
MYSQL_PASSWORD=dataagent123
MYSQL_DATABASE=dataagent

# 大模型 API
LLM_API_KEY=sk-xxx
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-v4-flash

# Embedding API（知识库检索）
EMBEDDING_API_KEY=sk-xxx
EMBEDDING_BASE_URL=https://api.siliconflow.cn/v1/embedding
EMBEDDING_MODEL=Qwen/Qwen3-VL-Embedding-8B

# 向量库
VECTOR_STORE=qdrant
QDRANT_URL=http://172.20.10.3:6333

# 上传与查询限制
MAX_UPLOAD_MB=100
QUERY_ROW_LIMIT=500
SQL_TIMEOUT_SECONDS=15
```

- 留空 `LLM_API_KEY` 时系统使用本地演示模式（预置规则兜底）
- API Key 仅由后端读取，前端不接收或显示
- 修改配置后需重启后端

## 默认账号

| 账号 | 密码 | 角色 |
|------|------|------|
| `liuze` | `18437431` | 初始管理员 |

首次启动自动创建演示数据集（近 120 天区域/品类/渠道经营数据，4800 条记录）和 5 条业务知识片段。

## 演示路径建议

1. 登录系统 → 查看工作台仪表盘
2. 进入「数据源」→ 查看预置演示数据或上传 CSV
3. 进入「智能分析」→ 输入 `"统计各地区销售额"` → 查看图表、洞察、SQL、数据表
4. 追问 `"按产品类别拆分"` 或 `"只看华东地区"` → 验证多轮上下文继承
5. 上传 PDF/Word 文件 → `"总结这份文档的关键信息"` → 验证文件分析
6. 进入「业务知识库」→ 查看知识片段 → 在分析中问 `"销售额的计算口径是什么"`
7. 进入「历史对话」→ 恢复之前的会话
8. 进入「账户管理」→ 创建子管理员并分配数据源权限
9. 进入「审计日志」→ 查看操作记录并导出 Excel
10. 点击分析结果页的「导出报告」→ 下载 HTML / Word / PDF

## API 端点概览

| 路径 | 说明 |
|------|------|
| `GET /api/v1/health` | 健康检查 |
| `GET /api/v1/config/status` | 系统配置状态 |
| `POST /api/v1/auth/login` | 登录 |
| `GET /api/v1/auth/me` | 当前用户信息 |
| `GET /api/v1/datasets` | 数据源列表 |
| `POST /api/v1/datasets/upload` | 上传 CSV/Excel |
| `POST /api/v1/datasets/mysql/import` | MySQL 导入 |
| `POST /api/v1/agent/chat` | 非流式分析 |
| `POST /api/v1/agent/chat/stream` | SSE 流式分析 |
| `POST /api/v1/agent/chat/react` | ReAct Agent 分析 |
| `POST /api/v1/agent/chat/with-file` | 文件文档分析 |
| `POST /api/v1/agent/report/plan` | 分析报告规划 |
| `POST /api/v1/agent/report/pdf` | 多章节 PDF 导出 |
| `GET /api/v1/reports/{id}.html` | HTML 报告 |
| `GET /api/v1/reports/{id}.docx` | Word 报告 |
| `GET /api/v1/reports/{id}.pdf` | PDF 报告 |
| `GET /api/v1/reports/{id}.md` | Markdown 报告 |
| `GET /api/v1/sessions` | 历史会话列表 |
| `GET /api/v1/knowledge` | 知识库列表 |
| `POST /api/v1/knowledge/upload` | 上传知识文档 |
| `GET /api/v1/audit/logs` | 审计日志 |

完整 API 文档：[http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)

## 功能边界与演进

### 已实现
- CSV/Excel 上传与字段自动识别，MySQL 导入（含 SSL/SSH）
- 自然语言数据分析（5 条分析路径：规则流水线 / ReAct Agent / MCP / 文件分析 / 报告流）
- 只读 SQL 安全校验（表名白名单、危险语句拦截、CTE 识别）
- ReAct Agent 工具调用（7 个 Function Calling 工具）
- RAG 知识库问答（三路并行召回 + LLM 重排序 + 语义缓存）
- 文件文档智能分析（PDF/Word/MD/TXT，全文直读 + 分段提炼双模式）
- 多图表分析报告生成（HTML/Word/PDF/Markdown 导出）
- 多轮连续追问与对话上下文压缩
- 跨会话用户画像学习
- SSE 流式实时推送分析进度
- RBAC 管理员权限体系（数据集级 + 列级掩码）
- 全量审计日志与 Excel 导出
- 速率限制（登录/分析/上传）
- 错误消息脱敏与中文诊断建议
- Docker Compose 一键部署

### 待演进
- OIDC 企业单点登录
- Python 容器沙箱隔离执行
- 数据血缘自动采集
- 多模型路由与成本治理
- OpenTelemetry 可观测性
- 离线评测集与回归测试
