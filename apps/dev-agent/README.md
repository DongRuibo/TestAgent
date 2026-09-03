# TestAgent Dev Agent

这是一个单进程开发 Agent。`run_code`、表格和 Browser 工具在进程内直连腾讯 AGSX，
`web_search` 直连 Tavily；无需启动 Sandbox Gateway 或 Web Capability Service。
可选的长期记忆通过 `MEMORY_BASE_URL` 连接独立的 Mem0-primary Memory Service。
回答前，Agent 调用 `/v1/runtime/memory/retrieve` 获取当前用户的相关记忆，并始终把它标记为
`untrusted_memory_data`；回答成功后，Agent 调用 `/v1/runtime/memory/extract`，由 Mem0 从
用户消息和最终回答中提取、更新记忆，再写入 Elasticsearch。
默认 `MEMORY_RECALL_POLICY=adaptive`，明确的 Python/纯计算任务跳过召回。Web 短期对话
保存在本地 SQLite，Agent 按完整轮次和字符预算组装历史；记忆服务失败不会阻断正常回答。
可选的 `search_knowledge_base` 通过 `RAG_BASE_URL` 只读连接固定知识库。
仓库仍保留两个独立服务，供其他 Agent 复用或后续恢复服务化部署。

可选的 LazyTT MCP Client 默认读取根目录下被 Git 忽略的
`.local/lazytt-mcp.env`。启用后，Agent 启动时通过标准 `tools/list` 发现 allowlist
中的远程工具，并以 `mcp__lazytt__<tool>` 名称合并进现有工具列表。
当前材料场景开放 `area_calculate`、`paint_calculate`、`tile_calculate` 和
`flooring_calculate`，每个任务中每项最多调用一次。Client 会使用
OIDC Client Credentials 自动申请并提前刷新短期 Token。局域网 HTTP 配置仅用于受信隔离网络。
远程 `area_calculate` 可用时，Agent 不再同时暴露本地
`calculate_material_quantities`，避免两个重叠工具竞争；关闭 MCP 后会恢复本地工具。
Web 应用在第一次健康检查或第一次对话时执行一次 `tools/list`，随后复用发现结果和
Token 缓存；修改 MCP 地址或 allowlist 后需要重启 Web 应用。`GET /api/health` 会返回
`mcp_enabled`、`mcp_configured`、`mcp_connected` 和 `mcp_tools`。

```dotenv
MCP_ENABLED=true
MCP_SERVER_NAME=lazytt
MCP_SERVER_URL=http://<MCP_HOST>:8080/mcp
OIDC_TOKEN_URL=http://<MCP_HOST>:8090/realms/lazytt-mcp/protocol/openid-connect/token
OIDC_CLIENT_ID=<YOUR_AGENT_CLIENT_ID>
OIDC_CLIENT_SECRET=<REDACTED>
MCP_TOOL_ALLOWLIST=area_calculate,paint_calculate,tile_calculate,flooring_calculate
MCP_CALL_TIMEOUT_SECONDS=30
```

```bash
uv run testagent
```

根目录 `main.py` 保留兼容启动方式。
