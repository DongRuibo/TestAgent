# TestAgent Sandbox Gateway

独立的 Provider-neutral Sandbox HTTP 服务，负责鉴权、租户/Agent 归属、任务策略、
Provider 路由、Artifact、Receipt 和清理。Provider 密钥只配置在本服务。

本地开发：

```bash
uv run testagent-sandbox-gateway
```

默认地址为 `127.0.0.1:8787`。当前 SQLite、本地 Artifact 和进程内任务执行模型适合
POC/内部低风险使用，生产部署仍需外部数据库、对象存储和持久化队列。
