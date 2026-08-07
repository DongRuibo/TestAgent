"""LLM Client 的离线单元测试，不会访问真实模型服务。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from llm.client import LLMClient, LLMConfig


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


def test_config_requires_api_key_and_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证缺少必填环境变量时，会一次报告所有缺失项。"""
    # 清除宿主环境中的同名变量，保证测试结果不受开发者电脑配置影响。
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)

    with pytest.raises(RuntimeError, match="OPENAI_API_KEY, OPENAI_MODEL"):
        LLMConfig.from_env()
