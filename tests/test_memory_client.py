from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest

from memory.client import MemoryClientError, MemoryConfig, MemoryHttpClient


def _config() -> MemoryConfig:
    return MemoryConfig(
        base_url="http://127.0.0.1:18081",
        timeout_seconds=5,
        tenant_id="elasticsearch-poc-tenant",
        user_id="semantic-demo",
        automatic_extraction=True,
    )


def test_acquire_context_retrieves_mem0_memories_as_untrusted_items() -> None:
    memory_id = str(uuid4())
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == "/v1/runtime/memory/retrieve"
        assert request.headers["x-tenant-id"] == "elasticsearch-poc-tenant"
        assert request.headers["x-user-id"] == "semantic-demo"
        assert "authorization" not in request.headers
        body = json.loads(request.content)
        assert body == {"query": "怎么回答？", "limit": 6}
        return httpx.Response(
            200,
            json={
                "tenant_id": "elasticsearch-poc-tenant",
                "user_id": "semantic-demo",
                "backend": "mem0",
                "memories": [
                    {
                        "id": memory_id,
                        "memory": "用户偏好中文回答。",
                        "score": 0.91,
                    }
                ],
            },
        )

    client = MemoryHttpClient(
        _config(),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    context = client.acquire_context(query="怎么回答？")

    assert len(requests) == 1
    assert context.items == (
        {
            "kind": "memory",
            "text": "用户偏好中文回答。",
            "memory_id": memory_id,
            "score": 0.91,
            "trust": "untrusted_memory_data",
        },
    )
    assert '"instructions_executable":false' in context.prompt_data()


def test_extract_turn_submits_full_turn_to_mem0_runtime() -> None:
    episode_id = str(uuid4())
    attempt_id = str(uuid4())

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/runtime/memory/extract"
        assert request.headers["x-tenant-id"] == "elasticsearch-poc-tenant"
        assert request.headers["x-user-id"] == "semantic-demo"
        assert request.headers["idempotency-key"] == (
            f"testagent-runtime-extract-{attempt_id}"
        )
        body = json.loads(request.content)
        assert body["session_id"] == episode_id
        assert body["turn_id"] == attempt_id
        assert body["user_content"] == "分析销售文件"
        assert body["assistant_content"] == "销售额增长 20%。"
        assert body["verified_tool_results"] == [
            {
                "tool_call_id": "call-1",
                "tool_name": "analyze_spreadsheet",
                "content": '{"success": true}',
            }
        ]
        assert body["outcome"] == "succeeded"
        return httpx.Response(
            202,
            json={
                "tenant_id": "elasticsearch-poc-tenant",
                "user_id": "semantic-demo",
                "backend": "mem0",
                "memories": [{"id": str(uuid4()), "event": "ADD"}],
            },
        )

    client = MemoryHttpClient(
        _config(),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = client.extract_turn(
        episode_id=episode_id,
        attempt_id=attempt_id,
        user_content="分析销售文件",
        assistant_content="销售额增长 20%。",
        verified_tool_results=[
            {
                "tool_call_id": "call-1",
                "tool_name": "analyze_spreadsheet",
                "content": '{"success": true}',
            }
        ],
        completed_at=datetime(2026, 8, 12, tzinfo=UTC),
    )

    assert result == {"memory_count": 1}


def test_memory_error_does_not_include_provider_response_body() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(401, text="secret provider diagnostic")
    )
    client = MemoryHttpClient(_config(), http_client=httpx.Client(transport=transport))

    with pytest.raises(MemoryClientError) as error:
        client.ready()

    assert "HTTP 401" in str(error.value)
    assert "secret provider diagnostic" not in str(error.value)


def test_ready_accepts_empty_success_response() -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(204))
    client = MemoryHttpClient(_config(), http_client=httpx.Client(transport=transport))

    assert client.ready() is True


def test_context_rejects_invalid_mem0_memory() -> None:
    response = httpx.Response(
        200,
        json={
            "tenant_id": "elasticsearch-poc-tenant",
            "user_id": "semantic-demo",
            "memories": [{"id": str(uuid4()), "memory": "", "score": 0.5}],
        },
    )
    client = MemoryHttpClient(
        _config(),
        http_client=httpx.Client(
            transport=httpx.MockTransport(lambda _request: response)
        ),
    )

    with pytest.raises(MemoryClientError, match="缺少正文"):
        client.acquire_context(query="test")
