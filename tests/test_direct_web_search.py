"""Agent 直连 Tavily 搜索工具的离线测试。"""

from __future__ import annotations

import json

import httpx

from tools.web_search import (
    MAX_QUERY_CHARS,
    TavilySearchClient,
    TavilySearchConfig,
    web_search,
)


def _client(handler) -> TavilySearchClient:
    return TavilySearchClient(
        TavilySearchConfig(api_key="test-tavily-key"),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_direct_search_sends_bounded_tavily_request_and_normalizes_results() -> None:
    observed = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["url"] = str(request.url)
        observed["authorization"] = request.headers["authorization"]
        observed["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "Rust Releases",
                        "url": "https://BLOG.RUST-LANG.ORG/releases/#latest",
                        "content": "Release history",
                        "score": 0.9,
                        "published_date": "2026-08-01",
                    },
                    {"title": "Unsafe", "url": "file:///etc/passwd"},
                ]
            },
        )

    result = web_search("  Rust   release notes  ", client=_client(handler))

    assert result["success"] is True
    assert result["provider"] == "tavily"
    assert result["trust"] == "untrusted"
    assert result["instructions_executable"] is False
    assert result["results"] == [
        {
            "provider": "tavily",
            "rank": 1,
            "title": "Rust Releases",
            "url": "https://blog.rust-lang.org/releases/",
            "domain": "blog.rust-lang.org",
            "snippet": "Release history",
            "published_at": "2026-08-01",
            "provider_score": 0.9,
        }
    ]
    assert observed["url"] == "https://api.tavily.com/search"
    assert observed["authorization"] == "Bearer test-tavily-key"
    assert observed["payload"] == {
        "query": "Rust release notes",
        "search_depth": "basic",
        "topic": "general",
        "max_results": 5,
        "include_answer": False,
        "include_raw_content": False,
        "include_images": False,
        "auto_parameters": False,
    }


def test_direct_search_validates_query_before_network() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(500)

    client = _client(handler)
    assert web_search("   ", client=client)["error_type"] == "INVALID_ARGUMENTS"
    assert web_search("x" * (MAX_QUERY_CHARS + 1), client=client)["error_type"] == "INVALID_ARGUMENTS"
    assert calls == []


def test_direct_search_converts_timeout_and_rate_limit_to_structured_errors() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    timeout_result = web_search("Rust", client=_client(timeout))
    assert timeout_result == {
        "success": False,
        "error_type": "TAVILY_TIMEOUT",
        "message": "Tavily Search 请求超时。",
        "retryable": True,
    }

    limited = web_search(
        "Rust",
        client=_client(lambda _request: httpx.Response(429, headers={"Retry-After": "17"})),
    )
    assert limited["error_type"] == "TAVILY_ERROR"
    assert limited["retryable"] is True
    assert limited["retry_after_seconds"] == 17
