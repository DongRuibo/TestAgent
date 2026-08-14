# TestAgent Web Tools

把独立 Web Capability Service 暴露为 `web_search` 和 `web_fetch` Agent Tools。
Python 包只需要服务地址，不持有 Tavily、Brave 或 Exa 密钥。

```python
from testagent_web_tools import WebCapabilityTools

web = WebCapabilityTools.from_env()
tool_definitions = web.tool_definitions
tool_handlers = web.tool_handlers
```
