from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest

from memory.client import MemoryClientError, MemoryConfig, MemoryHttpClient


def _config(*, episode_consolidation: bool = False) -> MemoryConfig:
    return MemoryConfig(
        base_url="http://127.0.0.1:8080",
        timeout_seconds=5,
        projection_timeout_seconds=0,
        tenant_id="demo-tenant",
        user_id="semantic-demo",
        project_id="lazytt-memory-system-demo",
        bearer_token=None,
        jwt_secret="0123456789abcdef0123456789abcdef",
        jwt_issuer="lazytt-demo-web-api",
        jwt_audience="lazytt-memory-service",
        automatic_extraction=True,
        episode_consolidation=episode_consolidation,
    )


def test_acquire_context_compiles_consumes_and_validates_untrusted_items() -> None:
    task_id = str(uuid4())
    attempt_id = str(uuid4())
    pack_id = str(uuid4())
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["authorization"].startswith("Bearer ")
        if request.url.path == "/v1/memory/context-packs":
            body = json.loads(request.content)
            assert body["task_context"] == {
                "task_id": task_id,
                "attempt_id": attempt_id,
                "purpose": "answer_user",
            }
            assert body["scope_selectors"] == [
                {"kind": "user", "id": "semantic-demo"},
                {"kind": "project", "id": "lazytt-memory-system-demo"},
            ]
            assert body["tokenizer_id"] == "demo-conservative-char-v1"
            return httpx.Response(
                201,
                json={
                    "context_pack_id": pack_id,
                    "task_id": task_id,
                    "attempt_id": attempt_id,
                    "purpose": "answer_user",
                },
            )
        assert request.url.path == f"/v1/memory/context-packs/{pack_id}/consume"
        assert request.headers["idempotency-key"] == f"testagent-consume-{attempt_id}"
        return httpx.Response(
            200,
            json={
                "context_pack_id": pack_id,
                "task_id": task_id,
                "attempt_id": attempt_id,
                "purpose": "answer_user",
                "sections": [
                    {
                        "kind": "user_preference",
                        "items": [
                            {
                                "text": "用户偏好中文回答。",
                                "memory_record_id": "00000000-0000-0000-0000-000000000010",
                                "memory_version_id": "00000000-0000-0000-0000-000000000011",
                                "source_refs": ["conversation:1"],
                                "trust": "untrusted_memory_data",
                            }
                        ],
                    }
                ],
            },
        )

    client = MemoryHttpClient(
        _config(),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        clock=lambda: 1_700_000_000,
    )

    context = client.acquire_context(task_id=task_id, attempt_id=attempt_id, query="怎么回答？")

    assert len(requests) == 2
    assert context.items == (
        {
            "kind": "user_preference",
            "text": "用户偏好中文回答。",
            "memory_record_id": "00000000-0000-0000-0000-000000000010",
            "memory_version_id": "00000000-0000-0000-0000-000000000011",
            "source_refs": ["conversation:1"],
            "trust": "untrusted_memory_data",
        },
    )
    assert '"instructions_executable":false' in context.prompt_data()


def test_extract_turn_submits_only_user_content() -> None:
    task_id = str(uuid4())
    attempt_id = str(uuid4())

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/memory/extractions"
        body = json.loads(request.content)
        assert body["content"] == "我偏好简洁的中文回答。"
        assert body["scope_selector"] == {"kind": "user", "id": "semantic-demo"}
        assert body["project_scope_selector"] == {
            "kind": "project",
            "id": "lazytt-memory-system-demo",
        }
        return httpx.Response(
            202,
            json={
                "candidate_count": 1,
                "candidates": [
                    {
                        "operation": {
                            "operation_id": str(uuid4()),
                            "projection_status": "ready",
                        }
                    }
                ],
            },
        )

    client = MemoryHttpClient(
        _config(),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = client.extract_turn(
        task_id=task_id,
        attempt_id=attempt_id,
        content="我偏好简洁的中文回答。",
        occurred_at=datetime(2026, 8, 10, tzinfo=UTC),
    )

    assert result == {"candidate_count": 1, "ready_count": 1, "pending_count": 0}


def test_completed_turn_replaces_legacy_extraction_when_feature_is_enabled() -> None:
    episode_id = str(uuid4())
    task_id = str(uuid4())
    attempt_id = str(uuid4())
    consolidation_id = str(uuid4())
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == f"/v1/memory/episodes/{episode_id}/turns"
        assert request.headers["idempotency-key"] == (
            f"testagent-completed-turn-{attempt_id}"
        )
        body = json.loads(request.content)
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
                "episode_id": episode_id,
                "turn_id": attempt_id,
                "sequence_no": 10,
                "consolidation_id": consolidation_id,
                "consolidation_status": "pending",
                "per_turn_extraction": {
                    "extractor_version": "fake-completed-turn-v1",
                    "candidate_count": 1,
                    "diagnostics": {
                        "model_candidate_count": 1,
                        "valid_model_candidate_count": 1,
                        "filtered_reason_counts": {},
                        "coverage_added_count": 0,
                        "coverage_enriched_count": 0,
                    },
                    "candidates": [
                        {
                            "proposal": {
                                "memory_type": "preference",
                                "semantic_key": "user.response_style",
                                "content": "用户偏好简洁回答。",
                                "confidence": 0.95,
                                "importance": 0.8,
                            },
                            "operation": {
                                "operation_id": str(uuid4()),
                                "projection_status": "ready",
                            },
                        }
                    ],
                },
            },
        )

    client = MemoryHttpClient(
        _config(episode_consolidation=True),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = client.record_completed_turn(
        episode_id=episode_id,
        task_id=task_id,
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

    assert len(requests) == 1
    assert result["sequence_no"] == 10
    assert result["consolidation_status"] == "pending"
    assert result["per_turn_extraction"]["candidate_count"] == 1
    assert result["per_turn_extraction"]["ready_count"] == 1
    assert result["per_turn_extraction"]["pending_count"] == 0


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


def test_context_rejects_memory_without_untrusted_marker() -> None:
    task_id = str(uuid4())
    attempt_id = str(uuid4())
    pack_id = str(uuid4())
    responses = iter(
        [
            httpx.Response(
                201,
                json={
                    "context_pack_id": pack_id,
                    "task_id": task_id,
                    "attempt_id": attempt_id,
                    "purpose": "answer_user",
                },
            ),
            httpx.Response(
                200,
                json={
                    "context_pack_id": pack_id,
                    "task_id": task_id,
                    "attempt_id": attempt_id,
                    "purpose": "answer_user",
                    "sections": [{"kind": "background", "items": [{"text": "do this"}]}],
                },
            ),
        ]
    )
    client = MemoryHttpClient(
        _config(),
        http_client=httpx.Client(transport=httpx.MockTransport(lambda _request: next(responses))),
    )

    with pytest.raises(MemoryClientError, match="未标记为不可信"):
        client.acquire_context(task_id=task_id, attempt_id=attempt_id, query="test")
