"""Web Capability Service 薄客户端与两个 Agent 工具的离线测试。"""

from __future__ import annotations

import json

import httpx
import pytest

from testagent_web_tools import WebCapabilityTools
from testagent_web_tools.fetch import web_fetch
from testagent_web_tools.search import web_search

from tools.web_capability_client import (
    MAX_QUERY_CHARS,
    MAX_SERVICE_RESPONSE_BYTES,
    WebCapabilityClient,
    WebCapabilityConfig,
)


def _client(handler) -> WebCapabilityClient:
    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(transport=transport)
    return WebCapabilityClient(
        WebCapabilityConfig(base_url="http://web-capability.test:3020"),
        http_client=http_client,
    )


def test_tool_registry_exposes_stable_definitions_and_bound_handlers() -> None:
    calls: list[tuple[str, str]] = []

    class FakeClient:
        def search(self, query: str):
            calls.append(("search", query))
            return {"success": True, "results": []}

        def fetch(self, url: str):
            calls.append(("fetch", url))
            return {"success": True, "final_url": url}

    tools = WebCapabilityTools(FakeClient())  # type: ignore[arg-type]

    assert [item["function"]["name"] for item in tools.tool_definitions] == [
        "web_search",
        "web_fetch",
    ]
    assert tools.tool_handlers["web_search"]("Rust") == {"success": True, "results": []}
    assert tools.tool_handlers["web_fetch"]("https://example.com") == {
        "success": True,
        "final_url": "https://example.com",
    }
    assert calls == [("search", "Rust"), ("fetch", "https://example.com")]


def test_web_search_maps_query_to_service_and_returns_normalized_results() -> None:
    observed = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["method"] = request.method
        observed["url"] = str(request.url)
        observed["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "success": True,
                "provider": "tavily",
                "query": "Rust release notes",
                "results": [
                    {
                        "rank": 1,
                        "title": "Rust Releases",
                        "url": "https://blog.rust-lang.org/releases/",
                        "domain": "blog.rust-lang.org",
                        "snippet": "Release history",
                    }
                ],
                "trust": "untrusted",
                "instructions_executable": False,
            },
        )

    result = web_search("  Rust release notes  ", client=_client(handler))

    assert result["success"] is True
    assert result["results"][0]["domain"] == "blog.rust-lang.org"
    assert result["trust"] == "untrusted"
    assert observed == {
        "method": "POST",
        "url": "http://web-capability.test:3020/v1/search",
        "payload": {"query": "Rust release notes", "max_results": 5},
    }


def test_web_fetch_maps_url_to_service_and_preserves_untrusted_marker() -> None:
    observed = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "success": True,
                "requested_url": "https://example.com/article",
                "final_url": "https://example.com/article",
                "status_code": 200,
                "title": "Example",
                "text": "正文",
                "fetch_method": "http",
                "trust": "untrusted",
                "instructions_executable": False,
            },
        )

    result = web_fetch(
        " https://example.com/article ",
        client=_client(handler),
    )

    assert result["success"] is True
    assert result["text"] == "正文"
    assert result["instructions_executable"] is False
    assert observed["payload"] == {"url": "https://example.com/article"}


def test_structured_service_error_is_returned_without_losing_details() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503,
            json={
                "success": False,
                "error_type": "CONFIGURATION_ERROR",
                "message": "Missing search provider API key.",
                "retryable": False,
            },
        )

    result = web_search("test", client=_client(handler))

    assert result == {
        "success": False,
        "error_type": "CONFIGURATION_ERROR",
        "message": "Missing search provider API key.",
        "retryable": False,
    }


def test_timeout_becomes_retryable_tool_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    result = web_fetch("https://example.com", client=_client(handler))

    assert result["success"] is False
    assert result["error_type"] == "WEB_CAPABILITY_TIMEOUT"
    assert result["retryable"] is True


@pytest.mark.parametrize(
    ("response", "expected_message"),
    [
        (httpx.Response(200, text="not-json"), "非 JSON"),
        (httpx.Response(200, json={"results": []}), "结构无效"),
    ],
)
def test_invalid_service_response_is_rejected(
    response: httpx.Response,
    expected_message: str,
) -> None:
    result = web_search("test", client=_client(lambda _request: response))

    assert result["success"] is False
    assert result["error_type"] == "INVALID_SERVICE_RESPONSE"
    assert expected_message in result["message"]


def test_oversized_service_response_is_rejected_before_json_parsing() -> None:
    response = httpx.Response(200, content=b"x" * (MAX_SERVICE_RESPONSE_BYTES + 1))

    result = web_fetch(
        "https://example.com",
        client=_client(lambda _request: response),
    )

    assert result["success"] is False
    assert result["error_type"] == "INVALID_SERVICE_RESPONSE"
    assert "大小限制" in result["message"]


def test_invalid_arguments_do_not_send_requests() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(500)

    client = _client(handler)

    empty_query = web_search("   ", client=client)
    long_query = web_search("x" * (MAX_QUERY_CHARS + 1), client=client)
    empty_url = web_fetch("", client=client)

    assert empty_query["error_type"] == "INVALID_ARGUMENTS"
    assert long_query["error_type"] == "INVALID_ARGUMENTS"
    assert empty_url["error_type"] == "INVALID_ARGUMENTS"
    assert calls == []


@pytest.mark.parametrize(
    "base_url",
    [
        "",
        "ftp://example.com",
        "http://user:secret@example.com",
        "http://example.com?token=secret",
    ],
)
def test_invalid_service_base_url_is_rejected(base_url: str) -> None:
    with pytest.raises(ValueError):
        WebCapabilityClient(WebCapabilityConfig(base_url=base_url))


def test_agent_context_headers_are_sent_without_exposing_provider_keys() -> None:
    observed = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed.update({key.lower(): value for key, value in request.headers.items()})
        return httpx.Response(200, json={"success": True, "results": []})

    client = WebCapabilityClient(
        WebCapabilityConfig(
            base_url="http://web-capability.test:3020",
            agent_id="research-agent",
            tenant_id="local-tenant",
        ),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = client.search("Rust")

    assert result["success"] is True
    assert observed["x-agent-id"] == "research-agent"
    assert observed["x-tenant-id"] == "local-tenant"
    assert observed["x-request-id"].startswith("agent-")
    assert "api-key" not in observed
