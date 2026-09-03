# TestAgent

这个仓库以 Monorepo 方式维护 Agent Runtime、Sandbox Gateway 和 Web Capability，
用于不依赖 Agent 框架、从底层实现并验证一个个人助手 Agent。

当前默认版本是 **V2：单进程多工具个人助手 Agent**。模型可以在对话中调用本地文件、
代码沙箱、表格分析、公开网页、知识库和受控业务工作流。Code、表格和 Browser 工具在
Agent 进程内直连腾讯 AGSX，搜索直连 Tavily，不需要启动本项目自己的 Gateway 或 Web
Capability Service。两个服务及其 SDK 仍保留在仓库中，供多 Agent 复用或后续恢复服务化部署。

## 当前目录

```text
.
├── apps/dev-agent/         # 默认单进程 Agent 与本地 Web UI
├── services/
│   ├── sandbox-gateway/    # FastAPI Gateway + Sandbox Runtime + Dockerfile
│   └── web-capability/     # Rust Search/Fetch 服务 + Dockerfile
├── packages/
│   ├── sandbox-contracts/  # Gateway/Client 共用的稳定协议模型
│   ├── sandbox-client/     # 可独立安装的同步/异步 Python SDK
│   ├── rpa-agent-module/   # 受控的本地浏览器与 RPA 工作流
│   └── web-capability-tools/ # Web Client、Tool schemas 与 handlers
├── contracts/              # 两个服务的版本化 OpenAPI 契约
├── deploy/compose.yaml     # 本地一键启动两个 Capability 服务
├── main.py                 # Dev Agent 兼容启动入口
├── scripts/
│   ├── agsx_smoke.py       # 最小 Code 云沙箱验证
│   ├── agsx_code_report.py # Code/文件/网络/超时/清理报告
│   ├── sales_analysis_poc.py # CSV → JSON/SVG/Receipt 闭环
│   ├── agsx_browser_poc.py # Browser CDP 与安全探针
│   └── agsx_cleanup.py     # 仅回收本项目标记的过期实例
├── fixtures/
│   └── sales.csv           # 固定销售分析样本
├── artifacts/              # POC 报告与产物（不提交 Git）
├── tests/
│   ├── test_agent_loop.py  # Agent Loop 数据流测试
│   ├── test_llm_client.py  # 模型客户端测试
│   ├── test_read_file.py   # 文件工具与路径安全测试
│   └── test_run_code.py    # AGSX 工具的离线生命周期测试
├── workspace/              # Dev Agent 读取和写入文件
├── examples/
│   └── gateway_client_demo.py # 其他 Agent 的最小接入示例
├── .env.example            # 配置示例
└── pyproject.toml          # uv Workspace 与统一开发依赖
```

模块化和发布边界详见[模块化架构与发布设计](doc/模块化架构与发布设计.md)。

给其他 Agent 或开发者交付模块时，分别参见：

- [Sandbox 模块下载与接入指南](doc/Sandbox_模块下载与接入指南.md)
- [Web Search 模块下载与接入指南](doc/Web_Search_模块下载与接入指南.md)

## 安装

项目使用 Python 3.11+ 和 `uv`：

```bash
uv sync
cp .env.example .env
```

然后编辑 `.env`：

```dotenv
OPENAI_API_KEY=你的密钥
OPENAI_MODEL=你的模型名
AGENT_TIMEZONE=Asia/Shanghai
TAVILY_API_KEY=你的_Tavily_密钥
E2B_API_KEY=e2b_格式的_AGSX_密钥
E2B_DOMAIN=ap-guangzhou.tencentags.com
AGSX_TEMPLATE=你的_Code_Tool_名称
AGSX_BROWSER_TEMPLATE=你的_Browser_Tool_名称
ARTIFACT_TOOL_ROOT=/absolute/path/to/@oai/artifact-tool
MEMORY_ENABLED=true
MEMORY_AUTO_EXTRACT=true
MEMORY_BASE_URL=http://127.0.0.1:18081
MEMORY_ENV_FILE=/absolute/path/to/lazytt-memory-system/.env
MEMORY_TENANT_ID=elasticsearch-poc-tenant
MEMORY_USER_ID=semantic-demo
MEMORY_RECALL_POLICY=adaptive
CONVERSATION_DB_PATH=.local/conversations.sqlite3
CONTEXT_MAX_CHARS=40000
CONTEXT_HISTORY_MAX_CHARS=16000
CONTEXT_TOOL_RESULT_MAX_CHARS=8000
RAG_ENABLED=true
RAG_BASE_URL=http://127.0.0.1:8082
RAG_KNOWLEDGE_BASE_ID=你的_TestAgent_知识库_UUID
RAG_ATTACHMENT_PDF_KNOWLEDGE_BASE_ID=你的_PDF附件知识库_UUID
RAG_ATTACHMENT_OFFICE_KNOWLEDGE_BASE_ID=你的_Office附件知识库_UUID
RAG_ATTACHMENT_IMAGE_KNOWLEDGE_BASE_ID=你的_图片附件知识库_UUID
RAG_ATTACHMENT_MEDIA_KNOWLEDGE_BASE_ID=你的_音视频附件知识库_UUID
RAG_BEARER_TOKEN_FILE=/absolute/path/to/dev_token_testagent.jwt
RAG_RETRIEVAL_PROFILE=hybrid-default@1
RAG_ATTACHMENT_MAX_BYTES=104857600
```

如果使用 OpenAI 兼容服务，再配置：

```dotenv
OPENAI_BASE_URL=https://你的服务地址/v1
```

## 运行

默认 Agent 不依赖本项目的 `8787` 或 `3020` 服务，直接启动持续对话：

启用记忆时，需要先在独立仓库启动 LazyTT Memory Service：

```bash
cd /absolute/path/to/TestAgent
uv run python scripts/memory_backend.py up
curl http://127.0.0.1:18081/health/ready
```

该包装脚本根据 `MEMORY_ENV_FILE` 定位 Memory 仓库并启动 B 模式：Mem0 负责记忆提取、
更新和检索，Elasticsearch 保存向量；旧 governed PostgreSQL 后端不会启动。模型和
embedding 配置继续由 Memory 仓库自己的 `.env` 管理。停止服务且保留记忆数据时运行
`uv run python scripts/memory_backend.py down`；不要使用 `down --volumes`。

`MEMORY_RECALL_POLICY=adaptive` 会跳过明确的 Python、纯计算、指定文件/URL 等确定性任务，
历史、偏好和项目连续性任务会调用 `/v1/runtime/memory/retrieve`；无法明确分类的普通任务
默认召回，避免漏掉相关记忆。回答成功后，Agent 调用 `/v1/runtime/memory/extract`，提交
用户消息、可见最终回答和成功且受限的工具结果，由 Mem0 自动提取并更新记忆。Web 使用
`conversation_id` 作为稳定 session，CLI 在 `/clear` 后开始新 session。记忆内容始终以
`untrusted_memory_data` 注入；隐藏推理、失败工具日志不会进入提取请求。召回或提取失败
都不会阻断本轮回答。

RAG 是独立的文档证据系统，与 Memory 分工：Memory 保存用户偏好和项目连续性，
RAG 保存可核验的文档原文。没有对话附件时，`search_knowledge_base` 检索长期知识库；
当前对话存在已就绪附件时，PDF、Office/文本、图片和音视频会分别进入四个附件知识库，
检索请求同时限定这些知识库 UUID 和当前 Conversation 的文档 UUID，避免跨对话读取临时
文件。四个附件库必须使用同一个 JWT Scope 和 Embedding Profile，UUID 必须彼此不同且
不能复用长期知识库，并且必须全部配置；缺少任意一个时网页附件入口保持禁用。上传和
移除只能由用户在聊天框操作，不向模型暴露写能力。
短期 JWT 过期后，在 RAG 仓库重新运行
`tools/issue_dev_token.py` 覆盖 Token 文件即可；工具每次调用都会重新读取该文件。

Web 对话由服务端 SQLite 保存，浏览器只保存 `conversation_id`。刷新页面会恢复当前对话；
“清空对话”会删除该 Conversation 的短期消息，但不会删除 LazyTT 长期记忆。历史按完整
user/assistant 轮次和字符预算裁剪，当前 Attempt 的 tool call/result 只在本轮存在。

```bash
UV_CACHE_DIR=/tmp/testagent-uv-cache uv run python main.py
```

无参数启动后可以连续追问；CLI 进程最多保留最近 20 条用户/助手消息，Agent 再按字符预算
选择完整轮次：

```text
/clear  清空当前上下文
/exit   退出对话
```

自动化脚本仍可直接传入一句话，完成后退出：

```bash
uv run python main.py "读取 example.md，并总结核心内容"
```

### 网页对话演示

启动本地网页 Agent：

```bash
UV_CACHE_DIR=/tmp/testagent-uv-cache uv run uvicorn web_agent.app:app \
  --host 127.0.0.1 --port 8000
```

浏览器打开：

```text
http://127.0.0.1:8000
```

页面支持流式回答、暂停保留、多会话历史、附件、结构化 Generation UI，以及可折叠的执行轨迹
和产物来源。模型、AGSX、Tavily、Memory 与 RAG 配置只保留在 Agent Python 进程，不会写入
网页代码。对话保存在服务端 SQLite 中，浏览器只保存当前 `conversation_id`；刷新页面或重启
服务后可以恢复。被暂停的回答会保留问题和已生成内容，但不会进入后续 Agent 历史或长期记忆。
这是本地个人助手界面，不应直接暴露到公网。

Agent Runtime 对每次任务执行独立的工具硬预算：默认 `web_search` 最多 2 次，
其他工具也有与成本和风险相匹配的上限。相同工具和等价参数
在一次任务中只真正执行一次，后续调用复用第一次结果且不消耗预算；达到上限后会向
模型返回 `TOOL_BUDGET_EXCEEDED`。构造 `AgentLoop` 时可用 `tool_budgets` 显式覆盖。
每次任务开始时还会按 `AGENT_TIMEZONE` 动态读取并注入当前日期时间，确保“今天、最近、
最新、即将”等表达不依赖模型猜测；默认时区为 `Asia/Shanghai`。

运行测试：

```bash
uv run pytest
```

## Agent 工具演示

让当前 Agent 先通过 Tavily 搜索，再用 AGSX Browser 核实网页正文：

```bash
uv run python main.py "必须先调用 web_search 搜索 Rust 官方发布记录，再选择一个官方结果调用 browse_public_page 核实正文，最后列出来源 URL。"
```

通用表格分析 Agent：

```bash
uv run python main.py "必须调用 analyze_spreadsheet 分析 fixtures/sales.csv，告诉我行列数、字段类型、缺失值、异常值、产物，以及沙箱是否销毁。"
```

经营简报 Agent：

```bash
uv run python main.py "必须调用 generate_business_briefing 分析 fixtures/sales.csv，并告诉我 JSON、图表、Markdown、PDF、PPTX 和执行回执是否生成。"
```

公开网页 Browser Agent：

```bash
uv run python main.py "必须调用 browse_public_page 打开 https://example.com，告诉我状态码、标题、正文摘要、截图路径、内容信任级别，以及沙箱是否销毁。"
```

只读检索 TestAgent 知识库：

```bash
uv run python main.py "必须调用 search_knowledge_base 查找合成 launch code，并标明文档名和页码。"
```

一次展示两个沙箱工具：

```bash
uv run python main.py "先调用 analyze_spreadsheet 分析 fixtures/sales.csv，再调用 browse_public_page 打开 https://example.com；最后分别汇报结果、Artifact 和沙箱销毁状态。"
```

`web_search` 在 Agent 进程内直接调用 Tavily，只负责发现候选 URL；重要结论继续用
`browse_public_page` 在一次性 AGSX Browser 中核实正文。查询和网页正文都不得包含或
诱导泄露密钥、个人信息、内部源码或客户机密。

`analyze_spreadsheet` 和 `generate_business_briefing` 只允许读取 `workspace/` 或
`fixtures/` 中的 CSV/XLSX。所有搜索结果
和网页正文都是不可信数据；Browser 当前仅允许公开、低敏 POC，不支持登录、付款
或后台系统操作。

如果需要把 Sandbox 或 Web 能力交付给其他 Agent，仓库中保留的独立服务仍可按
[Sandbox 接入指南](doc/沙箱网关接入指南.md)和
[Web Search 接入指南](doc/Web_Search_模块下载与接入指南.md)部署；它们不是当前 Agent 的运行依赖。

## 第四周固定评测

运行全套固定用例并生成 JSON/Markdown 报告：

```bash
uv run python -m evals.runner --suite all
```

只做低成本烟雾评测（每个套件取 1 个用例）：

```bash
uv run python -m evals.runner --suite all --max-cases 1
```

评测结果写入 `artifacts/evals/<run-id>/`，统计 case pass rate、task success
rate、清理率、P50/P95、95% Wilson 区间和成本估算。预期失败（例如超时、错误 CSV）
只有在返回指定失败类型时才算用例通过。评测器支持多轮和受控并发，但结果仍是
POC 证据，不代表生产 SLA。

可审计最终报告的逐实例回执、SHA256 和清理状态：

```bash
uv run python scripts/audit_evaluation_report.py \
  artifacts/evals/<run-id>/evaluation-report.json
```

当前固定矩阵为 Code 30 个、Browser 30 个、销售 3 个；Browser 30 个 URL 是
`example.com` 同源的 200/404 矩阵，主要验证导航、响应状态和清理，不代表 30 个不同网站。

PPIO 对照入口（使用独立凭据和 PPIO 官方 SDK）：

```bash
export PPIO_API_KEY=你的_PPIO_API_KEY
export PPIO_DOMAIN=sandbox.ppio.cn
export PPIO_TEMPLATE=code-interpreter-v1
export PPIO_BROWSER_TEMPLATE=browser-chromium
uv run python -m evals.runner --provider ppio-sandbox --suite code-sales --concurrency 3
```

PPIO Browser 对照：

```bash
uv run python -m evals.runner --provider ppio-sandbox --suite browser --concurrency 3
```

得到两份同口径报告后，可生成 Provider 差异表：

```bash
uv run python -m evals.compare \
  artifacts/evals/<agsx-run>/evaluation-report.json \
  artifacts/evals/<ppio-run>/evaluation-report.json
```

PPIO 配置、Code+销售先行对照和 Browser 前置条件见
`doc/PPIO_同场对照运行指南.md`。

## 腾讯云 AGSX Smoke Test

先在腾讯云控制台创建可用的 Code Interpreter 工具和 API Key，然后参考
`.env.example` 配置 `E2B_API_KEY`、`E2B_DOMAIN` 与 `AGSX_TEMPLATE`。
`E2B_API_KEY` 应复制控制台展示的 `e2b_...` 兼容格式；当前 E2B SDK 会在
发送网络请求前拒绝 `ark_...` 格式。

运行独立的云沙箱验证：

```bash
uv run python scripts/agsx_smoke.py
```

脚本会创建一个限时沙箱，在远端执行 Python 并写入随机 `/tmp` 文件，验证该
文件没有出现在本机，最后无论成功或失败都会调用 `kill()` 销毁实例。

在 `SANDBOX` 网络模式下运行单实例综合 POC：

```bash
uv run python scripts/agsx_poc.py
```

综合 POC 会验证销售数据分析、Shell、沙箱内文件读写、本地密钥未传入、
公网出站被阻止，并记录创建、执行、销毁和总耗时。

## 第一至三周 POC

### 1. Code POC 可归档报告

```bash
uv run python scripts/agsx_code_report.py
```

验证 Python、JavaScript、二进制文件上传下载及 SHA256、SANDBOX 网络、执行
超时和最终销毁。报告写入 `artifacts/code-poc-*/code-poc-report.json`。网络
探针区分 DNS 解析与 TCP 公网连接，避免把“能解析域名”误判成“能访问公网”。

### 2. 销售数据分析闭环

```bash
uv run python scripts/sales_analysis_poc.py
```

脚本归档输入 CSV，上传到 AGSX，在沙箱内只使用 Python 标准库计算销售汇总，
下载 `summary.json` 和 `sales-chart.svg`，校验哈希，写出
`execution-receipt.json`，最后销毁实例。失败路径也会生成 Receipt。

### 3. Browser Sandbox Go/No-Go

先在腾讯云控制台创建 Browser 类型、PUBLIC 网络、5 分钟生命周期的低敏 POC
工具，并把工具名配置为：

```dotenv
AGSX_BROWSER_TEMPLATE=lazyx-browser-public-poc
```

当前 POC 脚本也以该名称作为默认值；显式配置环境变量便于以后切换工具。

然后运行：

```bash
uv run python scripts/agsx_browser_poc.py
```

脚本通过 CDP/Playwright 验证公开网页标题、正文和截图，并探测 Metadata、
IPv4/IPv6 回环地址、私网地址、重定向、跨实例 Cookie 与 Prompt Injection
不可信标记。本基础脚本对 DNS 重绑定只记录为 `inconclusive`，不会把它算成通过；
受控实验请使用下方的 DNS rebinding 脚本。即使全部通过，结论也只允许进入低风险 POC。

可用受控 `rbndr.us` 主机做 DNS 答案观测（只查询 DNS，不访问解析出的地址）：

```bash
uv run python scripts/dns_rebinding_probe.py \
  --host 7f000001.08080808.rbndr.us --samples 8 --interval-seconds 1
```

该观测只验证测试服务是否返回多个答案，不等同于 AGSX Browser 的 DNS rebinding
防护证明；云端 TOCTOU 仍需在具备受控内网 HTTP 服务的环境中专项验证。

具备受控服务后，可在 AGSX Browser 内记录每次导航的实际连接地址：

```bash
uv run python scripts/agsx_browser_dns_rebinding_poc.py \
  --host <hex-ip-a>.<hex-ip-b>.rbndr.us --port 80 --samples 8
```

也可以让项目自动创建 Code 公网端点和 Browser 本地端点，再做同一实验：

```bash
uv run python scripts/agsx_dns_rebinding_controlled_poc.py
```

回收超过指定时间、且带有本项目 `managed_by=lazyx-testagent` 标记的实例：

```bash
uv run python scripts/agsx_cleanup.py --max-age-seconds 600
```

清理脚本不会操作没有本项目标记的其他沙箱。

验证跨租户文件隔离、Metadata 绑定和超时清理：

```bash
uv run python scripts/agsx_isolation_poc.py
```

完成 smoke test 后，可以让模型自主决定调用 AGSX：

```bash
uv run python main.py "使用 Python 精确计算 123456789 的平方根，并给出结果"
```

V2 当前采用“一次工具调用一个沙箱实例”的生命周期，不会跨多次 `run_code`
调用保留变量或文件。

## 面向其他 Agent 的 Sandbox Gateway

如果希望多个 Agent 共用统一沙箱能力，不要让每个 Agent 直接持有 AGSX/PPIO 密钥；
启动项目内的 Gateway，由 Agent 通过 REST 或 Python SDK 调用。Gateway 负责鉴权、
租户隔离、文件交换、Provider 选择、Artifact、执行回执和最终销毁。

本地启动和接入说明见 [`doc/沙箱网关接入指南.md`](doc/沙箱网关接入指南.md)：

```bash
UV_CACHE_DIR=/tmp/testagent-uv-cache uv run uvicorn sandbox_gateway.factory:app \
  --host 127.0.0.1 --port 8787
UV_CACHE_DIR=/tmp/testagent-uv-cache uv run python -m examples.gateway_client_demo
```

Gateway V0 当前是单机实现（SQLite、本地 Artifact、进程内 API Key、线程池）；适合
 学习和内部试用，生产化还需要持久队列、对象存储、正式身份系统、配额和网络安全专项验证。

真实 Gateway 云端验证（每次只创建一个实例，必须显式确认）：

```bash
UV_CACHE_DIR=/tmp/testagent-uv-cache uv run python scripts/gateway_provider_e2e.py \
  --provider ppio-sandbox --task code --confirm-cloud
```

重启恢复和项目标签孤儿回收：

```bash
UV_CACHE_DIR=/tmp/testagent-uv-cache uv run python scripts/gateway_recovery.py \
  --max-age-seconds 600
```

## 演进路线

1. V0：封装 LLM Client，完成普通对话（已完成）
2. V1：加入 `read_file`，手写最小 Agent Loop（已完成）
3. V2：加入工具注册、AGSX `run_code` 和多工具分发（当前）
4. V3：加入校验、错误反馈、停止与重复检测
5. V4：加入上下文裁剪、任务状态和结构化摘要
6. V5：加入日志、固定任务集与评估指标

每一版都先通过测试和实际运行观察问题，再引入下一个机制。
