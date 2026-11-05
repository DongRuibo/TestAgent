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
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://127.0.0.1:18081/health/ready"
        return httpx.Response(204)

    transport = httpx.MockTransport(handler)
    client = MemoryHttpClient(_config(), http_client=httpx.Client(transport=transport))

    assert client.ready() is True


def test_separate_health_endpoint_keeps_retrieval_on_business_port(monkeypatch) -> None:
    monkeypatch.setenv("MEMORY_ENABLED", "true")
    monkeypatch.setenv("MEMORY_BASE_URL", "http://127.0.0.1:18081")
    monkeypatch.setenv("MEMORY_HEALTH_BASE_URL", "http://127.0.0.1:18082/")
    monkeypatch.setenv("MEMORY_TENANT_ID", "testagent-local")
    monkeypatch.setenv("MEMORY_USER_ID", "local-user")
    config = MemoryConfig.from_env()
    assert config is not None
    assert config.health_base_url == "http://127.0.0.1:18082"
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            assert str(request.url) == "http://127.0.0.1:18082/health/ready"
            return httpx.Response(200)
        assert str(request.url) == "http://127.0.0.1:18081/v1/runtime/memory/retrieve"
        assert request.headers["x-tenant-id"] == "testagent-local"
        assert request.headers["x-user-id"] == "local-user"
        return httpx.Response(200, json={
            "tenant_id": "testagent-local", "user_id": "local-user", "memories": [],
        })

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        client = MemoryHttpClient(config, http_client=http_client)
        assert client.ready() is True
        assert client.acquire_context(query="检查接口地址").items == ()
    assert len(requests) == 2


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


def governed_exchange(body, *, with_items=True):
    """合成与 Rust Manifest/Consume 契约一致的响应，不访问真实数据。"""
    from datetime import timedelta

    allowed_types = {"preference", "profile_fact", "semantic_fact", "episodic_summary"}
    assert isinstance(body.get("memory_types"), list) and body["memory_types"], "MEMORY_TYPE_REQUIRED"
    assert all(kind in allowed_types for kind in body["memory_types"]), "MEMORY_TYPE_UNSUPPORTED"
    now = datetime.now(UTC)
    manifest = {
        "context_pack_id": str(uuid4()), **body["task_context"],
        "authority_snapshot_id": "authority-v1", "policy_version": "policy-v1",
        "compiler_version": "compiler-v1", "ranking_version": "ranking-v1",
        "projection_generation": "generation-1", "reference_time": body["reference_time"] or now.isoformat(),
        "timezone": body["timezone"], "created_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
        "tokenizer_id": body["tokenizer_id"], "token_budget": body["token_budget"],
        "estimated_tokens": 30 if with_items else 0, "omission_summary": {},
        "item_count": int(with_items), "delivery_status": "compiled", "consumed_at": None,
    }
    item = {
        "memory_record_id": str(uuid4()), "memory_version_id": str(uuid4()),
        "text": "合成用户偏好中文。", "source_refs": ["synthetic:turn-1"],
        "effective_from": "2026-01-01T00:00:00Z",
        "event_time": {"start": "2026-01-01T00:00:00Z", "end": "2026-01-02T00:00:00Z",
                       "timezone": "Asia/Shanghai", "precision": "day"},
        "inclusion_reason": "relevant", "trust": "untrusted_memory_data",
    }
    pack = {key: value for key, value in manifest.items() if key not in {"item_count", "delivery_status"}}
    pack.update(consumed_at=now.isoformat(), sections=[{"kind": "user_preference", "items": [item] if with_items else []}])
    return manifest, pack


def governed_config(**overrides):
    from dataclasses import replace

    return replace(_config(), **{"backend_mode": "governed", "bearer_token": "synthetic-token", **overrides})


@pytest.mark.parametrize("with_items", [True, False])
def test_governed_compile_consume_and_provenance(with_items):
    task_id, attempt_id = str(uuid4()), str(uuid4())
    responses = []
    requests = []

    def handler(request):
        requests.append(request)
        body = json.loads(request.content)
        assert request.headers["authorization"] == "Bearer synthetic-token"
        if len(requests) == 1:
            assert request.url.path == "/v1/memory/context-packs"
            assert body["task_context"] == {"task_id": task_id, "attempt_id": attempt_id, "purpose": "answer_user"}
            assert body["scope_selectors"] == [{"kind": "user", "id": "semantic-demo"}]
            assert body["tokenizer_id"] == "demo-conservative-char-v1"
            responses.extend(governed_exchange(body, with_items=with_items))
            return httpx.Response(201, json=responses[0])
        assert request.url.path == f"/v1/memory/context-packs/{responses[0]['context_pack_id']}/consume"
        assert request.headers["idempotency-key"] == f"testagent-consume-{attempt_id}"
        assert body == {"task_context": {"task_id": task_id, "attempt_id": attempt_id, "purpose": "answer_user"}}
        return httpx.Response(200, json=responses[1])

    client = MemoryHttpClient(governed_config(), http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    context = client.acquire_context(query="上次偏好", task_id=task_id, attempt_id=attempt_id, reference_time=datetime.now(UTC))
    assert len(requests) == 2
    assert context.context_pack_id == responses[0]["context_pack_id"]
    assert len(context.items) == int(with_items)
    if with_items:
        assert context.items[0]["memory_version_id"] == responses[1]["sections"][0]["items"][0]["memory_version_id"]
        assert context.items[0]["source_refs"] == ["synthetic:turn-1"]
        assert '"instructions_executable":false' in context.prompt_data()


@pytest.mark.parametrize("stage,field,value", [
    ("manifest", "task_id", str(uuid4())), ("manifest", "attempt_id", str(uuid4())),
    ("manifest", "purpose", "other"), ("manifest", "token_budget", 99),
    ("manifest", "tokenizer_id", "other"), ("manifest", "timezone", "UTC"),
    ("manifest", "expires_at", "2000-01-01T00:00:00Z"),
    ("manifest", "reference_time", "2000-01-01T00:00:00Z"),
    ("manifest", "item_count", True), ("manifest", "delivery_status", "invalid"),
    *[("consume", field, str(uuid4())) for field in ("task_id", "attempt_id", "context_pack_id")],
    *[("consume", field, "other") for field in ("purpose", "authority_snapshot_id", "policy_version", "compiler_version", "ranking_version", "projection_generation")],
    ("consume", "token_budget", 99), ("consume", "estimated_tokens", 2),
    ("consume", "timezone", "UTC"), ("consume", "tokenizer_id", "other"),
    *[("consume", field, "2000-01-01T00:00:00Z") for field in ("reference_time", "created_at", "expires_at", "consumed_at")],
    ("consume", "omission_summary", {"budget": 1}),
    ("item", "source_refs", []), ("item", "trust", "system_instruction"),
    ("item", "text", ""), ("item", "memory_version_id", "invalid"),
    ("item", "event_time", {"start": "2026-01-02T00:00:00Z", "end": "2026-01-01T00:00:00Z", "timezone": "UTC", "precision": "day"}),
    ("consume", "sections", []),
])
def test_governed_rejects_manifest_consume_contract_drift(stage, field, value):
    responses = []

    def handler(request):
        if request.url.path == "/v1/memory/context-packs":
            responses.extend(governed_exchange(json.loads(request.content)))
            if stage == "manifest":
                responses[0][field] = value
            return httpx.Response(201, json=responses[0])
        if stage == "consume":
            responses[1][field] = value
        elif stage == "item":
            responses[1]["sections"][0]["items"][0][field] = value
        return httpx.Response(200, json=responses[1])

    client = MemoryHttpClient(governed_config(), http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(MemoryClientError, match="契约不匹配"):
        client.acquire_context(query="上次", task_id=str(uuid4()), attempt_id=str(uuid4()), reference_time=datetime.now(UTC))


def test_governed_completed_turn_preserves_execution_and_candidate_meaning():
    task_id, attempt_id, episode_id = (str(uuid4()) for _ in range(3))
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.path == f"/v1/memory/episodes/{episode_id}/turns"
        assert request.headers["idempotency-key"] == f"testagent-completed-turn-{attempt_id}"
        body = json.loads(request.content)
        assert (body["task_id"], body["attempt_id"], body["turn_id"]) == (task_id, attempt_id, attempt_id)
        assert body["timezone"] == "UTC"
        assert body["project_scope_selector"] is None
        assert body["verified_tool_results"][0]["tool_name"] == "synthetic"
        return httpx.Response(202, json={"episode_id": episode_id, "turn_id": attempt_id, "sequence_no": 1,
                                          "per_turn_extraction": {"candidate_count": 0, "candidates": []}})

    client = MemoryHttpClient(governed_config(), http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    kwargs = dict(episode_id=episode_id, task_id=task_id, attempt_id=attempt_id, user_content="问题", assistant_content="回答",
                  verified_tool_results=[{"tool_call_id": "call1", "tool_name": "synthetic", "content": "verified"}],
                  completed_at=datetime.now(UTC), timezone="UTC")
    assert client.extract_turn(**kwargs) == {"candidate_count": 0}
    assert client.extract_turn(**kwargs) == {"candidate_count": 0}
    assert requests[0].content == requests[1].content


def test_governed_config_and_health_do_not_disclose_bearer(monkeypatch):
    monkeypatch.setenv("MEMORY_ENABLED", "true")
    monkeypatch.setenv("MEMORY_BACKEND_MODE", "governed")
    monkeypatch.setenv("MEMORY_BEARER_TOKEN", "synthetic-token")
    config = MemoryConfig.from_env()
    assert config.backend_mode == "governed" and "synthetic-token" not in repr(config)
    with pytest.raises(MemoryClientError, match="Bearer"):
        MemoryHttpClient(governed_config(base_url="https://memory.example", bearer_token=None))
    with pytest.raises(MemoryClientError, match="HTTPS"):
        MemoryHttpClient(governed_config(base_url="http://memory.example"))

    def handler(request):
        assert str(request.url) == "http://127.0.0.1:18082/health/ready"
        assert "authorization" not in request.headers
        return httpx.Response(204)

    client = MemoryHttpClient(governed_config(health_base_url="http://127.0.0.1:18082"),
                              http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert client.ready()


def test_governed_synthetic_identity_matrix_propagates_scope_without_context_reuse():
    from dataclasses import replace

    identities = [(tenant, user) for tenant in ("tenant-a", "tenant-b") for user in ("alice", "bob")]
    seen = []
    for tenant, user in identities:
        responses = []
        marker = f"synthetic:{tenant}:{user}"

        def handler(request):
            assert request.headers["x-tenant-id"] == tenant
            assert request.headers["x-user-id"] == user
            body = json.loads(request.content)
            if request.url.path == "/v1/memory/context-packs":
                assert body["scope_selectors"] == [{"kind": "user", "id": user}]
                responses.extend(governed_exchange(body))
                responses[1]["sections"][0]["items"][0]["text"] = marker
                return httpx.Response(201, json=responses[0])
            return httpx.Response(200, json=responses[1])

        config = replace(governed_config(), tenant_id=tenant, user_id=user)
        client = MemoryHttpClient(config, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
        context = client.acquire_context(query="上次", task_id=str(uuid4()), attempt_id=str(uuid4()))
        assert context.items[0]["text"] == marker
        assert all(other not in context.prompt_data() for other in seen)
        seen.append(marker)
    assert len(set(seen)) == 4


def test_governed_compile_passes_real_entrypoint_memory_type_gate_before_consume():
    reached_consume = False
    responses = []

    def handler(request):
        nonlocal reached_consume
        body = json.loads(request.content)
        if request.url.path == "/v1/memory/context-packs":
            # 按后端入口规则独立拒绝空类型或不支持的类型，不能让宽松夹具替服务端放行。
            requested = body.get("memory_types")
            if not isinstance(requested, list) or not requested:
                return httpx.Response(400, json={"code": "MEMORY_TYPE_REQUIRED"})
            if any(kind not in {"preference", "profile_fact", "semantic_fact", "episodic_summary"} for kind in requested):
                return httpx.Response(400, json={"code": "MEMORY_TYPE_UNSUPPORTED"})
            responses.extend(governed_exchange(body))
            return httpx.Response(201, json=responses[0])
        reached_consume = True
        return httpx.Response(200, json=responses[1])

    client = MemoryHttpClient(governed_config(), http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    context = client.acquire_context(query="记得上次", task_id=str(uuid4()), attempt_id=str(uuid4()))
    assert reached_consume
    assert len(context.items) == 1
