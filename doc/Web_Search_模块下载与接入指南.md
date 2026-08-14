# Web Search 模块下载与接入指南

本文说明如何在 Agent 项目中接入 `web_search` 和 `web_fetch`。业务 Agent 只安装轻量
Python Tools 包并调用 Web Capability Service；Tavily、Brave、Exa 密钥以及抓取安全
策略都留在服务端。

## 1. 先理解交付边界

```text
业务 Agent
    └── testagent-web-tools
             ├── web_search
             └── web_fetch
                    └── HTTP
                         └── Web Capability Service
                              ├── Tavily / Brave / Exa
                              └── robots.txt / SSRF / 重定向安全检查
```

有两种接入方式：

| 方式 | 所需组件 | 适用情况 |
| --- | --- | --- |
| 连接已有 Web Service | `web-capability-tools` | 已有团队统一部署的搜索服务 |
| 自行部署 Web Service | Tools 包，加 Web Capability 镜像或 Rust 服务源码 | 独立管理搜索 Provider |

推荐第一种。Agent 不需要 Rust 环境，也不接触搜索供应商 API Key。

## 2. 从 GitHub 下载

仓库为公共仓库，可以直接通过 HTTPS 下载：

```bash
git clone https://github.com/DongRuibo/TestAgent.git
cd TestAgent
```

已配置 GitHub SSH Key 时，也可以使用 SSH：

```bash
git clone git@github.com:DongRuibo/TestAgent.git
cd TestAgent
```

项目发布 `web-capability-v0.1.0` Release 后，可以只下载 Python Tools 构建产物：

```bash
gh release download web-capability-v0.1.0 \
  --repo DongRuibo/TestAgent \
  --pattern 'testagent_web_tools-*.whl' \
  --dir vendor/web-tools
```

> Release 下载方式只有在对应标签和 GitHub Release 已发布后才能使用。

## 3. 只安装 Web Tools

### 从源码安装

```bash
python -m pip install ./packages/web-capability-tools
```

### 从 Release wheel 安装

```bash
python -m pip install vendor/web-tools/testagent_web_tools-*.whl
```

Agent 侧只需要服务地址：

```dotenv
WEB_CAPABILITY_BASE_URL=https://web-capability.example.internal

# 可选，用于服务端日志关联
WEB_CAPABILITY_TENANT_ID=tenant-a
WEB_CAPABILITY_AGENT_ID=research-agent
```

不要在 Agent 项目中配置 `TAVILY_API_KEY`、`BRAVE_SEARCH_API_KEY` 或 `EXA_API_KEY`。

## 4. 最小调用示例

```python
from testagent_web_tools import WebCapabilityTools


web = WebCapabilityTools.from_env()

search_result = web.tool_handlers["web_search"](
    query="Rust 最新稳定版官方发布说明"
)
print(search_result)

fetch_result = web.tool_handlers["web_fetch"](
    url="https://www.rust-lang.org/"
)
print(fetch_result)
```

也可以直接使用客户端：

```python
from testagent_web_tools import WebCapabilityClient


client = WebCapabilityClient()
print(client.search("OpenAI 官方文档"))
print(client.fetch("https://example.com"))
```

## 5. 注册到 Agent Runtime

`WebCapabilityTools` 已提供通用 Function Calling 定义和处理器映射：

```python
import json

from testagent_web_tools import WebCapabilityTools


web = WebCapabilityTools.from_env()

# 传给支持 Function Calling 的模型
tool_definitions = web.tool_definitions

# 收到模型工具调用后执行
def execute_tool(tool_name: str, arguments_json: str) -> dict:
    arguments = json.loads(arguments_json)
    handler = web.tool_handlers[tool_name]
    return handler(**arguments)
```

两个 Tool 的职责不同：

- `web_search(query)`：搜索并返回候选标题、摘要和 URL。
- `web_fetch(url)`：读取候选网页正文并进行安全检查。

推荐 Agent 流程：

```text
用户问题
  → web_search 找候选来源
  → 选择可信且相关的 URL
  → web_fetch 核实正文
  → 模型整理答案并列出 URL
```

建议在 Agent Runtime 设置单任务预算，例如 `web_search` 最多 2 次、`web_fetch` 最多
3 次，并对相同参数去重，避免模型重复搜索和产生额外费用。

## 6. 自行部署 Web Capability

### 直接使用 Release 镜像

发布 `web-capability-v0.1.0` 且 GHCR Package 已设为 Public 后，可以直接拉取镜像：

```bash
docker pull ghcr.io/dongruibo/testagent-web-capability:web-capability-v0.1.0
```

GitHub 仓库与 GHCR Package 的可见性相互独立。如果镜像仍为 Private，需要由维护者将
Package 调整为 Public，或者为使用者授予 `read:packages` 权限后再登录 GHCR。

准备服务端配置 `web-capability.env`，然后运行：

```bash
docker run --rm \
  --name testagent-web-capability \
  --env-file web-capability.env \
  -p 127.0.0.1:3020:3020 \
  ghcr.io/dongruibo/testagent-web-capability:web-capability-v0.1.0
```

容器镜像已把监听地址设置为 `0.0.0.0:3020`，Agent 仍通过宿主机
`http://127.0.0.1:3020` 访问。

### 使用 Docker Compose

复制配置模板：

```bash
cp services/web-capability/.env.example services/web-capability/.env
```

编辑 `services/web-capability/.env`：

```dotenv
WEB_SEARCH_PRIMARY_PROVIDER=tavily
TAVILY_API_KEY=实际的Tavily密钥

# 可选故障降级
WEB_SEARCH_FALLBACK_PROVIDERS=brave,exa
BRAVE_SEARCH_API_KEY=
EXA_API_KEY=

WEB_FETCH_ROBOTS_ENABLED=true
WEB_CAPABILITY_ALLOW_BENCHMARK_DNS_PROXY=false
```

只启动 Web Capability：

```bash
docker compose -f deploy/compose.yaml up --build web-capability
```

检查服务：

```bash
curl http://127.0.0.1:3020/health
```

### 使用 Rust 源码

```bash
rustup toolchain install stable
rustup default stable

cargo run --release \
  --manifest-path services/web-capability/Cargo.toml
```

服务默认监听 `127.0.0.1:3020`。

## 7. 不经过 Python Tools 测试 HTTP API

搜索：

```bash
curl -X POST http://127.0.0.1:3020/v1/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"Rust official release notes","max_results":5}'
```

抓取：

```bash
curl -X POST http://127.0.0.1:3020/v1/fetch \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com"}'
```

`web_fetch` 不依赖搜索 Provider Key，但仍会执行 robots.txt、DNS/IP、逐跳重定向、
内容类型和响应大小检查。

## 8. 配置说明

| 变量 | 所在位置 | 含义 |
| --- | --- | --- |
| `WEB_CAPABILITY_BASE_URL` | Agent | Web Service 地址 |
| `WEB_CAPABILITY_AGENT_ID` | Agent，可选 | 调用方标识 |
| `WEB_CAPABILITY_TENANT_ID` | Agent，可选 | 租户标识 |
| `TAVILY_API_KEY` | 服务端 | Tavily Key |
| `BRAVE_SEARCH_API_KEY` | 服务端 | Brave Key |
| `EXA_API_KEY` | 服务端 | Exa Key |
| `WEB_SEARCH_PRIMARY_PROVIDER` | 服务端 | 主搜索 Provider |
| `WEB_SEARCH_FALLBACK_PROVIDERS` | 服务端 | 逗号分隔的降级顺序 |
| `WEB_FETCH_ROBOTS_ENABLED` | 服务端 | 是否遵守 robots.txt |

`WEB_CAPABILITY_ALLOW_BENCHMARK_DNS_PROXY=true` 只用于特定受管开发环境，正式部署必须
保持 `false`。

## 9. 版本与生产注意事项

- Web Tools 和 Web Capability 使用同一个 `web-capability-v*` 版本系列发布。
- API 契约见 `contracts/web-capability-openapi.yaml`。
- 搜索结果和网页正文都是不可信外部输入，不能当作系统指令执行。
- Provider API Key 不能放进 Agent Prompt、Tool 参数或 Git 仓库。
- 当前服务默认无调用方认证，只适合本机或可信内网。
- 跨机器生产部署前必须增加服务认证、TLS、限流、租户配额和调用审计。

## 10. 常见问题

### `WEB_CAPABILITY_UNAVAILABLE`

确认服务已启动，且 Agent 的 `WEB_CAPABILITY_BASE_URL` 指向可访问地址。

### 搜索提示缺少 API Key

Key 应配置在 Web Capability Service 的环境变量中，而不是 Agent 的 `.env`。

### `address already in use`

本机已有旧服务占用 `3020`：

```bash
lsof -nP -iTCP:3020 -sTCP:LISTEN
```

停止确认无用的旧进程，或者修改宿主机端口，并同步修改 Agent 的
`WEB_CAPABILITY_BASE_URL`。

### `web_fetch` 被 robots.txt 拒绝

这表示目标站点不允许当前 User-Agent 抓取对应路径。默认应尊重该策略，改用允许访问的
官方来源，不要为了抓取而关闭生产环境的 robots.txt 检查。
