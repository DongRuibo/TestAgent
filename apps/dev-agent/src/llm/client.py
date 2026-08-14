"""供项目各阶段复用的轻量大模型客户端封装。"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from openai import APIConnectionError, APITimeoutError, OpenAI


_MODEL_CONNECTION_ATTEMPTS = 3
_MODEL_CONNECTION_RETRY_BASE_SECONDS = 0.25


@dataclass(frozen=True)
class LLMConfig:
    """调用大模型所需的不可变配置。"""

    # API 密钥只用于创建 SDK 客户端，不会主动写入日志或输出。
    api_key: str
    # 具体模型名由环境变量决定，避免将某个供应商的模型写死在代码中。
    model: str
    # 为空时使用 OpenAI SDK 默认地址；也可指向兼容 OpenAI API 的服务。
    base_url: str | None = None

    @classmethod
    def from_env(cls) -> "LLMConfig":
        """从环境变量读取配置，并一次性报告所有缺失的必填项。"""
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        model = os.getenv("OPENAI_MODEL", "").strip()
        base_url = os.getenv("OPENAI_BASE_URL", "").strip() or None

        # 收集全部缺失项，让用户只需修改一次配置，而不是逐个遇到错误。
        missing = [
            name
            for name, value in (
                ("OPENAI_API_KEY", api_key),
                ("OPENAI_MODEL", model),
            )
            if not value
        ]
        if missing:
            names = ", ".join(missing)
            raise RuntimeError(f"Missing required environment variables: {names}")

        return cls(api_key=api_key, model=model, base_url=base_url)


class LLMStreamCancelled(RuntimeError):
    """模型流在上层请求停止后结束。"""


class LLMConnectionError(RuntimeError):
    """模型服务在有限重试后仍无法连接。"""


@dataclass(frozen=True)
class _StreamedFunction:
    name: str
    arguments: str


@dataclass(frozen=True)
class _StreamedToolCall:
    id: str
    function: _StreamedFunction
    type: str = "function"


@dataclass(frozen=True)
class _StreamedMessage:
    content: str | None
    tool_calls: list[_StreamedToolCall]

    def model_dump(self, *, exclude_none: bool = True) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant"}
        if self.content is not None or not exclude_none:
            message["content"] = self.content
        if self.tool_calls:
            message["tool_calls"] = [
                {
                    "id": item.id,
                    "type": item.type,
                    "function": {
                        "name": item.function.name,
                        "arguments": item.function.arguments,
                    },
                }
                for item in self.tool_calls
            ]
        return message


class LLMClient:
    """隔离模型供应商的调用细节，但不隐藏未来要实现的 Agent Loop。"""

    def __init__(self, config: LLMConfig, sdk_client: Any | None = None) -> None:
        """创建客户端；sdk_client 参数用于在测试中注入假客户端。"""
        self.config = config

        # 生产运行时创建真实 SDK 客户端，测试时则使用传入的假对象，避免联网。
        self._client = sdk_client or OpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
        )

    @classmethod
    def from_env(cls) -> "LLMClient":
        """使用环境变量中的配置创建客户端。"""
        return cls(LLMConfig.from_env())

    def chat(self, messages: Sequence[dict[str, Any]]) -> str:
        """向模型发送完整消息列表，并返回第一条文本回答。

        chat 保留为文本对话的便利入口；Agent Loop 使用 complete 获取包含
        tool_calls 的完整助手消息。
        """
        message = self.complete(messages)
        content = message.content

        # 空回答无法作为有效结果交给上层，统一转换成可识别的运行时错误。
        if not content:
            raise RuntimeError("The model returned an empty response.")
        return content

    def complete(
        self,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None = None,
    ) -> Any:
        """返回完整模型消息，让 Agent Loop 能读取文本或工具调用。

        V0 的 chat 方法只关心文本；V1 则需要读取 message.tool_calls，
        因此在这一层保留 SDK 返回的完整消息对象。
        """
        # 仅在确实提供工具时传入 tools，兼容不接受空工具列表的模型服务。
        request: dict[str, Any] = {
            "model": self.config.model,
            "messages": list(messages),
        }
        if tools:
            request["tools"] = list(tools)

        response = self._create_completion(request)
        return response.choices[0].message

    def _create_completion(self, request: dict[str, Any]) -> Any:
        """仅重试尚未产生模型输出的连接或超时错误。"""
        for attempt in range(_MODEL_CONNECTION_ATTEMPTS):
            try:
                return self._client.chat.completions.create(**request)
            except (APIConnectionError, APITimeoutError) as exc:
                if attempt == _MODEL_CONNECTION_ATTEMPTS - 1:
                    raise LLMConnectionError(
                        "模型服务暂时无法连接，已自动重试 2 次，请稍后重新执行。"
                    ) from exc
                time.sleep(_MODEL_CONNECTION_RETRY_BASE_SECONDS * (2**attempt))
        raise AssertionError("unreachable")

    def stream_complete(
        self,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None = None,
        *,
        on_text_delta: Callable[[str], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> Any:
        """流式读取一次模型消息，并重组可能被拆分的工具调用。"""
        request: dict[str, Any] = {
            "model": self.config.model,
            "messages": list(messages),
            "stream": True,
        }
        if tools:
            request["tools"] = list(tools)

        stream = self._create_completion(request)
        content_parts: list[str] = []
        tool_parts: dict[int, dict[str, str]] = {}
        try:
            for chunk in stream:
                if is_cancelled is not None and is_cancelled():
                    raise LLMStreamCancelled("Model stream cancelled")
                choices = getattr(chunk, "choices", None) or []
                if not choices:
                    continue
                delta = getattr(choices[0], "delta", None)
                if delta is None:
                    continue
                content = getattr(delta, "content", None)
                if isinstance(content, str) and content:
                    content_parts.append(content)
                    if on_text_delta is not None:
                        on_text_delta(content)
                for position, tool_call in enumerate(
                    getattr(delta, "tool_calls", None) or []
                ):
                    index = getattr(tool_call, "index", position)
                    if not isinstance(index, int):
                        index = position
                    accumulator = tool_parts.setdefault(
                        index,
                        {"id": "", "type": "function", "name": "", "arguments": ""},
                    )
                    call_id = getattr(tool_call, "id", None)
                    call_type = getattr(tool_call, "type", None)
                    function = getattr(tool_call, "function", None)
                    if isinstance(call_id, str):
                        accumulator["id"] += call_id
                    if isinstance(call_type, str) and call_type:
                        accumulator["type"] = call_type
                    name = getattr(function, "name", None)
                    arguments = getattr(function, "arguments", None)
                    if isinstance(name, str):
                        accumulator["name"] += name
                    if isinstance(arguments, str):
                        accumulator["arguments"] += arguments
            if is_cancelled is not None and is_cancelled():
                raise LLMStreamCancelled("Model stream cancelled")
        except (APIConnectionError, APITimeoutError) as exc:
            raise LLMConnectionError(
                "模型回答传输中断，请重新执行本次任务。"
            ) from exc
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                close()

        tool_calls = [
            _StreamedToolCall(
                id=parts["id"],
                type=parts["type"],
                function=_StreamedFunction(
                    name=parts["name"],
                    arguments=parts["arguments"],
                ),
            )
            for _, parts in sorted(tool_parts.items())
        ]
        content = "".join(content_parts) or None
        return _StreamedMessage(content=content, tool_calls=tool_calls)
