# Sandbox 模块下载与接入指南

本文说明如何在 Agent 项目中接入 TestAgent Sandbox。测试 Agent 不是运行依赖，业务
Agent 通过 Python SDK 调用 Sandbox Gateway；AGSX、PPIO 等云厂商密钥只保存在
Gateway 服务端。

## 1. 先选择使用方式

```text
业务 Agent
    └── sandbox-client + sandbox-contracts
             └── HTTP + Bearer Token
                      └── Sandbox Gateway
                              └── AGSX / PPIO
```

有两种接入方式：

| 方式 | 所需组件 | 适用情况 |
| --- | --- | --- |
| 连接已有 Gateway | `sandbox-contracts`、`sandbox-client` | 已有团队统一部署的 Sandbox 服务 |
| 自行部署 Gateway | 上述两个包，加 `sandbox-gateway` 服务或镜像 | 独立管理 Provider 密钥和运行数据 |

推荐第一种。它最轻量，也不会把云厂商密钥散落到每个 Agent 项目。

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

项目发布 `sandbox-v0.1.0` Release 后，可以只下载构建产物，无需下载整个仓库：

```bash
gh release download sandbox-v0.1.0 \
  --repo DongRuibo/TestAgent \
  --pattern '*.whl' \
  --dir vendor/sandbox
```

> Release 下载方式只有在对应标签和 GitHub Release 已发布后才能使用。

## 3. 只安装 Sandbox SDK

### 从源码安装

在仓库根目录执行：

```bash
python -m pip install \
  ./packages/sandbox-contracts \
  ./packages/sandbox-client
```

使用 `uv` 时，也可以把两个包作为本地依赖添加到业务项目的 `pyproject.toml`。

### 从 Release wheel 安装

```bash
python -m pip install vendor/sandbox/*.whl
```

Agent 侧只配置 Gateway 地址和分配给它的 Token：

```dotenv
SANDBOX_GATEWAY_URL=https://sandbox.example.internal
SANDBOX_GATEWAY_API_KEY=由服务管理员分配的Token
```

不要在 Agent 项目中配置 `E2B_API_KEY` 或 `PPIO_API_KEY`。

## 4. 最小调用示例

```python
import os

from sandbox_client import SandboxClient


with SandboxClient(
    os.environ["SANDBOX_GATEWAY_URL"],
    os.environ["SANDBOX_GATEWAY_API_KEY"],
) as sandbox:
    execution = sandbox.execute_code(
        "print(sum([1, 2, 3]))",
        language="python",
        timeout=60,
    )

    print(execution.status.value)
    print(execution.result)
    print(execution.artifacts)
```

SDK 当前提供：

- `execute_code()`：运行 Python 等代码。
- `analyze_spreadsheet_file()`：上传并分析 CSV/XLSX，可生成经营简报。
- `browse_public_page()`：使用 Browser Sandbox 访问公开网页。
- `get_execution()`、`wait()`、`cancel()`：管理异步执行。
- `download_artifact()`：下载执行生成的文件。
- `AsyncSandboxClient`：异步版本。

## 5. 封装成 Agent Tool

下面采用通用 Function Calling 格式，不依赖某个 Agent 框架：

```python
import os

from sandbox_client import SandboxClient


sandbox = SandboxClient(
    os.environ["SANDBOX_GATEWAY_URL"],
    os.environ["SANDBOX_GATEWAY_API_KEY"],
)

RUN_CODE_TOOL = {
    "type": "function",
    "function": {
        "name": "run_code",
        "description": "在隔离沙箱中运行 Python 代码。",
        "parameters": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "需要运行的 Python 代码"}
            },
            "required": ["code"],
            "additionalProperties": False,
        },
    },
}


def run_code(code: str) -> dict:
    result = sandbox.execute_code(code, language="python", timeout=60)
    return result.model_dump(mode="json")


tool_definitions = [RUN_CODE_TOOL]
tool_handlers = {"run_code": run_code}
```

Agent Runtime 收到模型的 `run_code` 调用后，解析参数并执行：

```python
result = tool_handlers[tool_name](**tool_arguments)
```

建议在 Agent Runtime 再增加调用预算、参数去重和执行超时，避免模型反复创建付费沙箱。

## 6. 自行部署 Gateway

### 直接使用 Release 镜像

发布 `sandbox-v0.1.0` 且 GHCR Package 已设为 Public 后，可以直接拉取镜像：

```bash
docker pull ghcr.io/dongruibo/testagent-sandbox-gateway:sandbox-v0.1.0
```

GitHub 仓库与 GHCR Package 的可见性相互独立。如果镜像仍为 Private，需要由维护者将
Package 调整为 Public，或者为使用者授予 `read:packages` 权限后再登录 GHCR。

准备只包含 Gateway 配置的 `sandbox.env`，然后运行：

```bash
docker volume create testagent-sandbox-data

docker run --rm \
  --name testagent-sandbox-gateway \
  --env-file sandbox.env \
  -p 127.0.0.1:8787:8787 \
  -v testagent-sandbox-data:/data \
  ghcr.io/dongruibo/testagent-sandbox-gateway:sandbox-v0.1.0
```

### 从源码部署

源码交付时，Gateway 构建还需要 Contracts，所以至少保留：

```text
packages/sandbox-contracts/
services/sandbox-gateway/
```

在完整仓库中准备根目录 `.env`：

```dotenv
GATEWAY_API_KEYS=demo-token|demo-tenant|demo-agent
GATEWAY_DATA_ROOT=artifacts/gateway

# 至少配置一个 Provider
E2B_API_KEY=实际的AGSX密钥
E2B_DOMAIN=ap-guangzhou.tencentags.com
AGSX_TEMPLATE=实际的Code Interpreter工具名
AGSX_BROWSER_TEMPLATE=实际的Browser工具名

# 或者配置 PPIO
# PPIO_API_KEY=实际的PPIO密钥
# PPIO_DOMAIN=sandbox.ppio.cn
# PPIO_TEMPLATE=code-interpreter-v1
```

只启动 Sandbox Gateway：

```bash
docker compose -f deploy/compose.yaml up --build sandbox-gateway
```

检查服务：

```bash
curl http://127.0.0.1:8787/health
```

本机源码启动方式：

```bash
uv sync --frozen
uv run testagent-sandbox-gateway
```

## 7. 接入验证

```bash
export SANDBOX_GATEWAY_URL=http://127.0.0.1:8787
export SANDBOX_GATEWAY_API_KEY=demo-token

python - <<'PY'
import os
from sandbox_client import SandboxClient

with SandboxClient(
    os.environ["SANDBOX_GATEWAY_URL"],
    os.environ["SANDBOX_GATEWAY_API_KEY"],
) as client:
    result = client.execute_code("print(2 + 2)")
    print(result.model_dump_json(indent=2))
PY
```

结果应包含 `status: "succeeded"`、Provider、执行回执和清理结果。

## 8. 版本与生产注意事项

- `sandbox-client` 与 `sandbox-contracts` 使用相同的 `0.x` 兼容版本范围。
- API 契约见 `contracts/sandbox-openapi.yaml`。
- Token 不能写进源码、镜像或 Git 仓库。
- 当前 Gateway 使用 SQLite、本地 Artifact 和进程内任务执行，适合 POC 和内部低风险使用。
- 正式生产需要补充统一身份认证、租户配额、持久队列、数据库、对象存储和完整审计。
- 不要把当前 Gateway 无保护地暴露到公网。

## 9. 常见问题

### `401 Unauthorized`

确认 Agent 的 `SANDBOX_GATEWAY_API_KEY` 与 Gateway 的 `GATEWAY_API_KEYS` 中 Token 一致。

### `No configured sandbox Provider`

Gateway 没有读到有效的 AGSX 或 PPIO 配置。检查 `.env` 是否传入服务容器。

### `address already in use`

本机已有旧 Gateway 占用 `8787`：

```bash
lsof -nP -iTCP:8787 -sTCP:LISTEN
```

停止确认无用的旧进程，或者修改 Compose 的宿主机端口和 Agent 的 Gateway URL。
