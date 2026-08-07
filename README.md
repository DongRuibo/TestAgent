# TestAgent

这个仓库以 Monorepo 方式维护 Agent Runtime、Sandbox Gateway 和 Web Capability，
用于不依赖 Agent 框架、从底层实现并验证一个开发者资料整理 Agent。

当前版本是 **V2：多工具 Agent + Sandbox Gateway**。模型可以自主选择 `read_file`、
`run_code`、`analyze_spreadsheet`、`generate_business_briefing`、`web_search`、
`web_fetch` 和 `browse_public_page`。Code、表格和 Browser 工具统一通过 Gateway，
由 Gateway 在 AGSX/PPIO 间按能力和网络档位选择 Provider；Agent 不持有云厂商密钥。

## 当前目录

```text
.
├── main.py                 # 命令行入口
├── agent/
│   └── loop.py             # 最小 Agent Loop
├── llm/
│   └── client.py           # 模型配置与调用封装
├── tools/
│   ├── read_file.py        # 限定在 workspace 内的读取工具
│   ├── gateway_sandbox.py  # Agent → Gateway 的 Code/表格/Browser 工具
│   ├── web_capability_client.py # 独立 Web 服务同步客户端
│   ├── web_search.py       # 搜索网页的原子工具
│   ├── web_fetch.py        # 静态网页抓取原子工具
│   └── browse_public_page.py # 公开网页 Browser Agent 工具
├── web-capability/         # Rust 搜索与静态抓取服务
│   ├── Cargo.toml
│   └── src/
├── sandbox_runtime/
│   ├── tencent_agsx.py     # AGSX Provider Adapter
│   ├── artifact_store.py   # 本地 Artifact 归档与 SHA256 校验
│   ├── spreadsheet_workflow.py # 通用 CSV/XLSX 沙箱分析
│   ├── report_renderer.py  # 受控 PDF/PPTX 经营简报渲染
│   ├── browser_workflow.py # 公开网页提取、截图、Receipt 与清理
│   └── browser_security.py # Browser 输入边界与不可信内容标记
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
├── workspace/              # 将来供 Agent 读取和写入文件
├── sandbox_gateway/        # 面向多个 Agent 的 Provider-neutral REST Gateway
├── sandbox_client/         # 同步/异步 Python SDK
├── examples/
│   └── gateway_client_demo.py # 其他 Agent 的最小接入示例
├── .env.example            # 配置示例
└── pyproject.toml          # Python 项目与依赖配置
```

`memory/`、`observability/` 会在对应阶段真正需要时再创建，避免一开始只有目录、没有机制。

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
WEB_CAPABILITY_BASE_URL=http://127.0.0.1:3020
SANDBOX_GATEWAY_URL=http://127.0.0.1:8787
SANDBOX_GATEWAY_API_KEY=与_Gateway_身份映射一致的本地_Token
GATEWAY_API_KEYS=同一个本地_Token|demo-tenant|demo-agent
ARTIFACT_TOOL_ROOT=/absolute/path/to/@oai/artifact-tool
```

如果使用 OpenAI 兼容服务，再配置：

```dotenv
OPENAI_BASE_URL=https://你的服务地址/v1
```

## 运行

先在终端 1 启动 Gateway：

```bash
UV_CACHE_DIR=/tmp/testagent-uv-cache uv run uvicorn sandbox_gateway.factory:app \
  --host 127.0.0.1 --port 8787
```

再在终端 2 启动持续对话：

```bash
UV_CACHE_DIR=/tmp/testagent-uv-cache uv run python main.py
```

无参数启动后可以连续追问；最近 20 条用户/助手消息会传给下一轮：

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

页面支持多轮文本上下文、三个演示快捷任务、模型/沙箱配置状态和可展开的工具执行轨迹。
模型与 AGSX 密钥只保留在 Python 服务端，不会写入网页代码。当前聊天历史只保存在
浏览器内存中，刷新页面后清空；这是本地演示界面，不应直接暴露到公网。

Agent Runtime 对每次任务执行独立的工具硬预算：默认 `web_search` 最多 2 次、
`web_fetch` 最多 3 次，其他工具也有与成本和风险相匹配的上限。相同工具和等价参数
在一次任务中只真正执行一次，后续调用复用第一次结果且不消耗预算；达到上限后会向
模型返回 `TOOL_BUDGET_EXCEEDED`。构造 `AgentLoop` 时可用 `tool_budgets` 显式覆盖。
每次任务开始时还会按 `AGENT_TIMEZONE` 动态读取并注入当前日期时间，确保“今天、最近、
最新、即将”等表达不依赖模型猜测；默认时区为 `Asia/Shanghai`。

运行测试：

```bash
uv run pytest
```

## Agent 集成演示

需要把 `web-capability/` 交付给其他 Agent 项目时，参见
[Web Search Tool 交付与 Agent 接入指南](doc/Web_Search_Tool_交付接入指南.md)。

先启动独立 Web Capability Service：

```bash
cd web-capability
cp .env.example .env
# 编辑 .env，默认填写 TAVILY_API_KEY；不填写时 web_fetch 仍可用。
cargo run
```

服务默认只监听 `127.0.0.1:3020`。搜索供应商密钥只属于服务端，不传给 Agent。
默认配置为 `WEB_SEARCH_PRIMARY_PROVIDER=tavily`；也可切换到 `brave` 或 `exa`，并通过
`WEB_SEARCH_FALLBACK_PROVIDERS` 显式配置故障降级。SearchService 提供总时间预算、自动
重试、进程内 TTL 缓存和结构化 usage 日志。本服务不会主动持久化搜索正文，但这不代表
上游零留存；查询中不得包含密钥、个人信息、内部源码或客户机密。

当前受管开发环境会把公网 DNS 映射到 `198.18.0.0/15`。仅在本机调试时，可以把
`web-capability/.env` 中的 `WEB_CAPABILITY_ALLOW_BENCHMARK_DNS_PROXY` 改为
`true`；该开关默认关闭、禁止用于生产，并且不会放行用户直接提交的该网段 IP。

如果当前开发机提示 `cargo: command not found`，使用已安装的稳定工具链：

```bash
PATH="/Users/drb/.rustup/toolchains/stable-aarch64-apple-darwin/bin:$PATH" cargo run
```

另开一个终端，让当前 Agent 搜索并核实网页正文：

```bash
uv run python main.py "必须先调用 web_search 搜索 Rust 官方发布记录，再选择一个官方结果调用 web_fetch 核实正文，最后列出来源 URL。"
```

只测试静态抓取：

```bash
uv run python main.py "必须调用 web_fetch 读取 https://example.com，并告诉我状态码、标题和正文摘要。"
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

一次展示两个沙箱工具：

```bash
uv run python main.py "先调用 analyze_spreadsheet 分析 fixtures/sales.csv，再调用 browse_public_page 打开 https://example.com；最后分别汇报结果、Artifact 和沙箱销毁状态。"
```

`web_search` 只负责发现候选 URL，重要结论应继续用 `web_fetch` 核实正文；
`web_fetch` 只接受安全的公开 HTTP/HTTPS URL，并在独立服务中执行 DNS/IP、
逐跳重定向、内容类型和响应大小检查。静态抓取失败、页面必须执行 JavaScript，
或明确需要截图时，才降级到 `browse_public_page`。

`analyze_spreadsheet` 和 `generate_business_briefing` 只允许读取 `workspace/` 或
`fixtures/` 中的 CSV/XLSX。所有搜索结果
和网页正文都是不可信数据；Browser 当前仅允许公开、低敏 POC，不支持登录、付款
或后台系统操作。

Rust 服务的单元与 API 契约测试：

```bash
PATH="/Users/drb/.rustup/toolchains/stable-aarch64-apple-darwin/bin:$PATH" \
  cargo test --manifest-path web-capability/Cargo.toml --all-targets
```

`web_fetch` 已执行 `robots.txt` 检查，并在页面重定向到新源站时重新检查目标源站；
仍需注意它是只读公开网页 MVP。详见 `web-capability/MVP_LIMITATIONS.md`。

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
