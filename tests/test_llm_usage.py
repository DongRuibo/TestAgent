"""实际用量与执行回执的离线验证，不读取凭据或访问模型。"""

from types import SimpleNamespace

import httpx
import pytest
from openai import APITimeoutError
from openai.types.chat import ChatCompletionMessage

from agent.context import MemoryRecallPolicy
from agent.loop import AgentLoop, READ_FILE_TOOL
from llm.client import LLMClient, LLMConfig


def test_usage_is_actual_and_missing_next_response_stays_unknown():
    responses = iter([
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
                        usage=SimpleNamespace(prompt_tokens=11, completion_tokens=3, total_tokens=14)),
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))]),
    ])
    sdk = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_: next(responses))))
    client = LLMClient(LLMConfig(api_key="synthetic", model="synthetic-model"), sdk_client=sdk)
    client.complete([{"role": "user", "content": "synthetic"}])
    assert client.last_call_receipt["usage"] == {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14}
    assert client.last_call_receipt["sdk_attempts"] == 1
    client.complete([{"role": "user", "content": "synthetic"}])
    assert client.last_call_receipt["usage"] is None


def test_loop_records_retries_and_tool_time_without_content(monkeypatch):
    replies = iter([
        APITimeoutError(request=httpx.Request("POST", "https://synthetic.invalid")),
        ChatCompletionMessage(role="assistant", content=None, tool_calls=[{
            "id": "call-1", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"synthetic.txt"}'}}]),
        ChatCompletionMessage(role="assistant", content="synthetic answer"),
    ])
    def create(**_):
        reply = next(replies)
        if isinstance(reply, Exception):
            raise reply
        return SimpleNamespace(choices=[SimpleNamespace(message=reply)], usage=None)
    monkeypatch.setattr("llm.client.time.sleep", lambda _: None)
    client = LLMClient(LLMConfig(api_key="synthetic-secret", model="synthetic-model"),
        sdk_client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    agent = AgentLoop(client, memory_recall_policy=MemoryRecallPolicy("off"),
        tool_definitions={"read_file": READ_FILE_TOOL}, tool_handlers={"read_file": lambda path: {"success": True, "content": "private synthetic content"}})
    assert agent.run("synthetic task") == "synthetic answer"
    receipt = agent.last_context_receipt.to_dict()
    assert [item["sdk_attempts"] for item in receipt["model_calls"]] == [2, 1]
    assert all(item["usage"] is None and item["duration_ms"] >= 0 for item in receipt["model_calls"])
    assert receipt["tool_calls"][0]["tool"] == "read_file"
    assert receipt["tool_calls"][0]["duration_ms"] >= 0
    assert "synthetic-secret" not in str(receipt)
    assert "private synthetic content" not in str(receipt)


def test_stream_usage_chunk_with_no_choices_is_recorded():
    chunks = [SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="ok", tool_calls=[]))]),
              SimpleNamespace(choices=[], usage=SimpleNamespace(prompt_tokens=5, completion_tokens=2, total_tokens=7))]
    client = LLMClient(LLMConfig(api_key="synthetic", model="synthetic-model"),
        sdk_client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_: iter(chunks)))))
    assert client.stream_complete([{"role": "user", "content": "synthetic"}]).content == "ok"
    assert client.last_call_receipt["usage"]["total_tokens"] == 7


@pytest.mark.parametrize("tool,task,source,attachments", [
    ("browse_public_page", "打开这个网页 https://example.com/page", "browser_prefetch", ()),
    ("search_knowledge_base", "总结这个附件", "attachment_prefetch", ("synthetic.pdf",)),
])
@pytest.mark.parametrize("success", [True, False])
def test_prefetch_is_observed_once_without_changing_tool_policy(tool, task, source, attachments, success):
    sdk = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_:
        SimpleNamespace(choices=[SimpleNamespace(message=ChatCompletionMessage(role="assistant", content="ok"))]))))
    client = LLMClient(LLMConfig(api_key="synthetic", model="synthetic-model"), sdk_client=sdk)
    calls = []
    def handler(**kwargs):
        calls.append(kwargs)
        return {"success": success, **({"content": "synthetic"} if success else {"error_type": "SYNTHETIC_ERROR"})}
    agent = AgentLoop(client, trace=None, memory_recall_policy=MemoryRecallPolicy("off"),
        attachment_filenames=attachments, tool_handlers={tool: handler})
    assert agent.run(task) == "ok"
    receipt = agent.last_context_receipt.to_dict()
    assert len(calls) == receipt["tool_result_count"] == len(receipt["tool_calls"]) == 1
    assert receipt["tool_calls"][0]["source"] == source
    assert receipt["tool_calls"][0]["duration_ms"] >= 0
    assert receipt["tool_calls"][0]["error_type"] == (None if success else "SYNTHETIC_ERROR")
