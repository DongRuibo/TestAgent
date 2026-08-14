# Web Capability Service

为 TestAgent 提供两个只读原子能力：

- `POST /v1/search`：通过 Tavily、Brave 或 Exa Search 查找候选网页。
- `POST /v1/fetch`：安全抓取公开静态网页并提取正文。

`GET /health` 返回服务状态、搜索是否已配置，以及开发 DNS 代理例外是否开启。

## 启动

```bash
cp .env.example .env
# 编辑 .env；默认使用 Tavily：填写 TAVILY_API_KEY。
cargo run
```

搜索供应商配置：

```dotenv
WEB_SEARCH_PRIMARY_PROVIDER=tavily
WEB_SEARCH_FALLBACK_PROVIDERS=brave,exa
TAVILY_API_KEY=你的密钥
BRAVE_SEARCH_API_KEY=可选
EXA_API_KEY=可选
```

`WEB_SEARCH_PROVIDER` 仍然兼容，可将主供应商设置为 `tavily`、`brave` 或 `exa`。
未配置主供应商时，服务按 Tavily、Brave、Exa 的 Key 存在顺序选择。只有显式配置
`WEB_SEARCH_FALLBACK_PROVIDERS` 后才会在可重试的上游错误时降级；认证错误和配置
错误不会切换。为兼容已有开发配置，服务也读取 `TAVILY_SEARCH_API_KEY`。
`web_fetch` 不需要任何搜索密钥。

页面抓取默认先读取目标源站的 `/robots.txt`。解析遵循 RFC 9309：优先匹配
`TestAgent-WebCapability`，否则使用 `*` 组；`Allow`/`Disallow` 按最长匹配处理，
同长度时 `Allow` 优先。robots 请求同样复用 URL 校验、DNS 解析和逐跳 IP 固定，
并使用进程内缓存（默认 1 小时，最长 24 小时）。可按需配置：

```dotenv
WEB_FETCH_ROBOTS_ENABLED=true
WEB_FETCH_ROBOTS_CACHE_TTL_SECONDS=3600
WEB_FETCH_ROBOTS_MAX_BODY_BYTES=524288
```

按照 RFC，robots 返回 4xx 时视为文件不可用并允许继续抓取；5xx、网络失败、
解析失败或超过 5 次重定向时，本服务选择 fail-closed，返回结构化的
`ROBOTS_UNREACHABLE`/`ROBOTS_PARSE_ERROR`，不会发送页面请求。

搜索请求由 `SearchService` 统一执行：具有 10 秒总时间预算、最多 3 次尝试、指数
退避和进程内 TTL 缓存。每次调用会输出不含原始 query/API Key/正文的结构化 usage 日志。
Python Agent 可通过 `WEB_CAPABILITY_AGENT_ID` 和 `WEB_CAPABILITY_TENANT_ID` 注入调用方
上下文；服务会生成或透传 `X-Request-ID`。

默认监听 `127.0.0.1:3020`。服务没有对公网暴露所需的认证机制，不应直接把监听
地址改为公网地址。

当前受管开发环境会把公网域名映射到 `198.18.0.0/15`，本机调试时可以显式设置：

```dotenv
WEB_CAPABILITY_ALLOW_BENCHMARK_DNS_PROXY=true
```

该开关默认关闭、禁止用于生产，而且只放行域名的 DNS 答案；用户直接提交
`198.18.0.0/15` IP 仍会被拒绝。

## 验证

```bash
cargo test --all-targets
cargo clippy --all-targets -- -D warnings
cargo fmt --all -- --check
```

所有搜索结果和网页正文都标记为不可信数据。本服务只在内存中处理供应商响应，
不主动持久化搜索正文；这不代表上游供应商零留存。不要在查询中放入密钥、个人信息、
内部源码或客户机密。生产前限制见 [MVP_LIMITATIONS.md](MVP_LIMITATIONS.md)。
