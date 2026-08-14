# TestAgent Sandbox Client

供 Agent 调用 Sandbox Gateway 的同步/异步 Python SDK。客户端只持有 Gateway 地址
和 Gateway Token，不接触 AGSX、PPIO 等 Provider 密钥。

```python
from sandbox_client import SandboxClient

with SandboxClient("http://127.0.0.1:8787", "demo-token") as sandbox:
    result = sandbox.execute_code("print(2 + 2)")
    print(result.status, result.result)
```
