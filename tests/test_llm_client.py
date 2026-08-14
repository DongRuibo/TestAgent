"""LLM Client 的离线单元测试，不会访问真实模型服务。"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from openai import APIConnectionError

from llm.client import LLMClient, LLMConfig, LLMConnectionError


class FakeCompletions:
    """模拟 OpenAI SDK 中 chat.completions 这一层接口。"""

    def __init__(self, content: str | None = "hello") -> None:
        # content 控制假模型返回的文本，也可以设为 None 来模拟空回答。
        self.content = content
        # 保存最近一次请求，方便断言业务代码传给 SDK 的参数是否正确。
        self.last_request = None

    def create(self, **kwargs):
        """记录请求参数，并构造与 SDK 响应结构相同的最小假对象。"""
        self.last_request = kwargs
        message = SimpleNamespace(content=self.content)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def make_client(content: str | None = "hello"):
    """创建注入假 SDK 的 LLMClient，供多个测试复用。"""
    completions = FakeCompletions(content)

    # SimpleNamespace 用最少代码复刻 client.chat.completions.create 调用链。
    sdk_client = SimpleNamespace(
        chat=SimpleNamespace(completions=completions),
    )
    config = LLMConfig(api_key="test-key", model="test-model")
    return LLMClient(config, sdk_client=sdk_client), completions


def test_chat_sends_messages_and_returns_text() -> None:
    """验证消息和模型名被正确发送，并原样返回模型文本。"""
    client, completions = make_client()
    messages = [{"role": "user", "content": "hi"}]

    result = client.chat(messages)

    assert result == "hello"
    assert completions.last_request == {
        "model": "test-model",
        "messages": messages,
    }


def test_chat_rejects_empty_model_response() -> None:
    """验证模型返回空内容时，客户端会抛出明确错误。"""
    client, _ = make_client(content=None)

    with pytest.raises(RuntimeError, match="empty response"):
        client.chat([{"role": "user", "content": "hi"}])


def test_stream_complete_emits_text_and_reassembles_tool_call_fragments() -> None:
    def chunk(*, content=None, tool_calls=None):
        delta = SimpleNamespace(content=content, tool_calls=tool_calls)
        return SimpleNamespace(choices=[SimpleNamespace(delta=delta)])

    def tool_fragment(index, *, call_id=None, name=None, arguments=None):
        return SimpleNamespace(
            index=index,
            id=call_id,
            type="function" if call_id else None,
            function=SimpleNamespace(name=name, arguments=arguments),
        )

    class FakeStream:
        def __init__(self):
            self.closed = False

        def __iter__(self):
            yield chunk(content="先")
            yield chunk(content="回答")
            yield chunk(
                tool_calls=[
                    tool_fragment(0, call_id="call_1", name="run_", arguments='{"co')
                ]
            )
            yield chunk(
                tool_calls=[tool_fragment(0, name="code", arguments='de":"1+1"}')]
            )

        def close(self):
            self.closed = True

    stream = FakeStream()
    completions = FakeCompletions()
    completions.create = lambda **kwargs: stream
    client = LLMClient(
        LLMConfig(api_key="test-key", model="test-model"),
        sdk_client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
    )
    deltas = []

    message = client.stream_complete(
        [{"role": "user", "content": "hi"}],
        tools=[{"type": "function", "function": {"name": "run_code"}}],
        on_text_delta=deltas.append,
    )

    assert deltas == ["先", "回答"]
    assert message.content == "先回答"
    assert message.tool_calls[0].id == "call_1"
    assert message.tool_calls[0].function.name == "run_code"
    assert message.tool_calls[0].function.arguments == '{"code":"1+1"}'
    assert stream.closed is True


def test_stream_complete_retries_connection_error_before_stream_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    delays = []

    class FakeStream:
        def __iter__(self):
            return iter([])

        def close(self):
            pass

    def create(**_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise APIConnectionError(request=httpx.Request("POST", "https://model.test"))
        return FakeStream()

    monkeypatch.setattr("llm.client.time.sleep", delays.append)
    client = LLMClient(
        LLMConfig(api_key="test-key", model="test-model"),
        sdk_client=SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        ),
    )

    message = client.stream_complete([{"role": "user", "content": "hi"}])

    assert attempts == 3
    assert delays == [0.25, 0.5]
    assert message.content is None


def test_stream_complete_classifies_exhausted_connection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    def create(**_kwargs):
        nonlocal attempts
        attempts += 1
        raise APIConnectionError(request=httpx.Request("POST", "https://model.test"))

    monkeypatch.setattr("llm.client.time.sleep", lambda _delay: None)
    client = LLMClient(
        LLMConfig(api_key="test-key", model="test-model"),
        sdk_client=SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        ),
    )

    with pytest.raises(LLMConnectionError, match="已自动重试 2 次"):
        client.stream_complete([{"role": "user", "content": "hi"}])

    assert attempts == 3


def test_config_requires_api_key_and_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证缺少必填环境变量时，会一次报告所有缺失项。"""
    # 清除宿主环境中的同名变量，保证测试结果不受开发者电脑配置影响。
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)

    with pytest.raises(RuntimeError, match="OPENAI_API_KEY, OPENAI_MODEL"):
        LLMConfig.from_env()
