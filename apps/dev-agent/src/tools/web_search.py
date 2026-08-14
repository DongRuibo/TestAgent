"""Agent 进程内直连 Tavily 的公开网页搜索工具。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx
from dotenv import load_dotenv

from testagent_dev_agent.paths import REPOSITORY_ROOT


TAVILY_SEARCH_ENDPOINT = "https://api.tavily.com/search"
MAX_QUERY_CHARS = 400
MAX_RESULTS = 5
MAX_RESPONSE_BYTES = 1024 * 1024


@dataclass(frozen=True)
class TavilySearchConfig:
    api_key: str
    endpoint: str = TAVILY_SEARCH_ENDPOINT
    timeout_seconds: float = 10.0

    @classmethod
    def from_env(cls) -> "TavilySearchConfig":
        load_dotenv(REPOSITORY_ROOT / ".env")
        api_key = os.getenv("TAVILY_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("Missing required environment variable: TAVILY_API_KEY")
        return cls(api_key=api_key)


class TavilySearchClient:
    def __init__(
        self,
        config: TavilySearchConfig,
        *,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.config = config
        self._client = http_client or httpx.Client(
            timeout=httpx.Timeout(config.timeout_seconds, connect=3.0),
            follow_redirects=False,
            trust_env=False,
            headers={"Accept": "application/json", "User-Agent": "TestAgent-Direct/0.1"},
        )

    def search(self, query: str) -> dict[str, Any]:
        if not isinstance(query, str) or not query.strip():
            return _error("INVALID_ARGUMENTS", "query 必须是非空字符串。")
        normalized_query = " ".join(query.split())
        if len(normalized_query) > MAX_QUERY_CHARS:
            return _error(
                "INVALID_ARGUMENTS",
                f"query 长度超过 {MAX_QUERY_CHARS} 字符。",
            )
        try:
            response = self._client.post(
                self.config.endpoint,
                headers={"Authorization": f"Bearer {self.config.api_key}"},
                json={
                    "query": normalized_query,
                    "search_depth": "basic",
                    "topic": "general",
                    "max_results": MAX_RESULTS,
                    "include_answer": False,
                    "include_raw_content": False,
                    "include_images": False,
                    "auto_parameters": False,
                },
            )
        except httpx.TimeoutException:
            return _error("TAVILY_TIMEOUT", "Tavily Search 请求超时。", retryable=True)
        except httpx.RequestError as exc:
            return _error(
                "TAVILY_UNAVAILABLE",
                f"Tavily Search 请求失败：{type(exc).__name__}",
                retryable=True,
            )

        if len(response.content) > MAX_RESPONSE_BYTES:
            return _error("INVALID_TAVILY_RESPONSE", "Tavily 响应超过 1 MiB 限制。")
        if not response.is_success:
            retryable = response.status_code == 429 or response.status_code >= 500
            result = _error(
                "TAVILY_ERROR",
                f"Tavily Search 返回 HTTP {response.status_code}。",
                retryable=retryable,
            )
            retry_after = response.headers.get("Retry-After", "").strip()
            if retry_after.isdigit():
                result["retry_after_seconds"] = int(retry_after)
            return result
        try:
            payload = response.json()
        except ValueError:
            return _error("INVALID_TAVILY_RESPONSE", "Tavily 返回了非 JSON 响应。")
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            return _error("INVALID_TAVILY_RESPONSE", "Tavily 响应结构无效。")

        results: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        for item in payload["results"]:
            normalized = _normalize_result(item, len(results) + 1)
            if normalized is None or normalized["url"] in seen_urls:
                continue
            seen_urls.add(normalized["url"])
            results.append(normalized)
            if len(results) >= MAX_RESULTS:
                break
        return {
            "success": True,
            "query": normalized_query,
            "provider": "tavily",
            "result_count": len(results),
            "results": results,
            "trust": "untrusted",
            "instructions_executable": False,
        }


def web_search(
    query: str,
    *,
    client: TavilySearchClient | None = None,
) -> dict[str, Any]:
    try:
        capability = client or default_tavily_client()
    except RuntimeError as exc:
        return _error("CONFIGURATION_ERROR", str(exc))
    return capability.search(query)


@lru_cache(maxsize=1)
def default_tavily_client() -> TavilySearchClient:
    return TavilySearchClient(TavilySearchConfig.from_env())


def _normalize_result(item: Any, rank: int) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    title = item.get("title")
    raw_url = item.get("url")
    if not isinstance(title, str) or not title.strip() or not isinstance(raw_url, str):
        return None
    parsed = urlparse(raw_url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username or parsed.password:
        return None
    normalized_url = urlunparse((
        parsed.scheme.lower(),
        parsed.netloc.lower(),
        parsed.path or "/",
        parsed.params,
        parsed.query,
        "",
    ))
    content = item.get("content")
    score = item.get("score")
    published_at = item.get("published_date")
    return {
        "provider": "tavily",
        "rank": rank,
        "title": title.strip(),
        "url": normalized_url,
        "domain": parsed.hostname.lower(),
        "snippet": content.strip() if isinstance(content, str) and content.strip() else None,
        "published_at": published_at if isinstance(published_at, str) else None,
        "provider_score": score if isinstance(score, int | float) and not isinstance(score, bool) else None,
    }


def _error(error_type: str, message: str, retryable: bool = False) -> dict[str, Any]:
    return {
        "success": False,
        "error_type": error_type,
        "message": message,
        "retryable": retryable,
    }


__all__ = [
    "MAX_QUERY_CHARS",
    "MAX_RESPONSE_BYTES",
    "TavilySearchClient",
    "TavilySearchConfig",
    "web_search",
]
