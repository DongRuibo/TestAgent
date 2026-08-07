"""供项目各阶段复用的轻量大模型客户端封装。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Sequence

from openai import OpenAI


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

        response = self._client.chat.completions.create(**request)
        return response.choices[0].message
