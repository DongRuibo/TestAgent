"""Web Capability Python Client 与 Agent Tool 注册接口。"""

from testagent_web_tools.client import (
    DEFAULT_BASE_URL,
    DEFAULT_TIMEOUT_SECONDS,
    MAX_QUERY_CHARS,
    MAX_SERVICE_RESPONSE_BYTES,
    MAX_URL_CHARS,
    WebCapabilityClient,
    WebCapabilityConfig,
    default_web_capability_client,
)
from testagent_web_tools.fetch import web_fetch
from testagent_web_tools.registry import WEB_FETCH_TOOL, WEB_SEARCH_TOOL, WebCapabilityTools
from testagent_web_tools.search import web_search

__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_QUERY_CHARS",
    "MAX_SERVICE_RESPONSE_BYTES",
    "MAX_URL_CHARS",
    "WEB_FETCH_TOOL",
    "WEB_SEARCH_TOOL",
    "WebCapabilityClient",
    "WebCapabilityConfig",
    "WebCapabilityTools",
    "default_web_capability_client",
    "web_fetch",
    "web_search",
]
