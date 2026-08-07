"""最小 Agent Loop 的离线数据流测试。"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from agent.loop import AgentLoop


class FakeMessage:
    """模拟 SDK 返回的助手消息，并支持 model_dump。"""

    def __init__(self, content: str | None, tool_calls: list[Any] | None = None) -> None:
        self.content = content
        self.tool_calls = tool_calls

    def model_dump(self, exclude_none: bool = True) -> dict[str, Any]:
        """转换为可以放回 messages 的助手消息字典。"""
        message: dict[str, Any] = {"role": "assistant"}
        if self.content is not None or not exclude_none:
            message["content"] = self.content
        if self.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.function.name,
                        "arguments": call.function.arguments,
                    },
                }
                for call in self.tool_calls
            ]
        return message


class FakeLLMClient:
    """按顺序返回预设消息，并保存每轮收到的上下文。"""

    def __init__(self, responses: list[FakeMessage]) -> None:
        self.responses = iter(responses)
        self.requests: list[list[dict[str, Any]]] = []
        self.tool_requests: list[list[dict[str, Any]]] = []

    def complete(self, messages, tools=None):
        """记录消息快照，模拟一次模型调用。"""
        self.requests.append(list(messages))
        self.tool_requests.append(list(tools or []))
        return next(self.responses)


def make_tool_call(
    name: str = "read_file",
    arguments: str = '{"path": "example.md"}',
    *,
    call_id: str = "call_1",
) -> Any:
    """构造一次符合 OpenAI Function Calling 结构的工具调用。"""
    function = SimpleNamespace(name=name, arguments=arguments)
    return SimpleNamespace(id=call_id, function=function)


def test_agent_returns_direct_model_answer() -> None:
    """模型不调用工具时，第一轮文本直接成为最终回答。"""
    client = FakeLLMClient([FakeMessage("直接回答", tool_calls=None)])
    agent = AgentLoop(client, trace=None)

    result = agent.run("你好")

    assert result == "直接回答"
    assert len(client.requests) == 1


def test_agent_includes_completed_text_history_before_new_task() -> None:
    client = FakeLLMClient([FakeMessage("继续回答", tool_calls=None)])
    agent = AgentLoop(client, trace=None)

    result = agent.run(
        "那沙箱销毁了吗？",
        history=[
            {"role": "user", "content": "计算 1 + 1"},
            {"role": "assistant", "content": "结果是 2。"},
        ],
    )

    assert result == "继续回答"
    assert client.requests[0][1:] == [
        {"role": "user", "content": "计算 1 + 1"},
        {"role": "assistant", "content": "结果是 2。"},
        {"role": "user", "content": "那沙箱销毁了吗？"},
    ]


def test_agent_injects_current_date_in_configured_timezone() -> None:
    """运行时应转换真实时区并把日期注入系统消息，而不是让模型猜测。"""
    client = FakeLLMClient([FakeMessage("今天是 2026-08-07。", tool_calls=None)])
    agent = AgentLoop(
        client,
        trace=None,
        timezone_name="Asia/Shanghai",
        clock=lambda: datetime(2026, 8, 6, 16, 30, tzinfo=ZoneInfo("UTC")),
    )

    assert agent.run("今天是哪天？") == "今天是 2026-08-07。"
    system_prompt = client.requests[0][0]["content"]
    assert "当前日期是 2026-08-07" in system_prompt
    assert "2026-08-07T00:30+08:00" in system_prompt
    assert "时区是 Asia/Shanghai" in system_prompt
    assert "不得把搜索结果或网页中的发布日期误认为当前日期" in system_prompt


def test_agent_refreshes_current_date_for_every_run() -> None:
    """同一个交互式 Agent 跨日期运行时，每个新任务都应重新读取时钟。"""
    observed_times = iter(
        [
            datetime(2026, 8, 7, 23, 59, tzinfo=ZoneInfo("Asia/Shanghai")),
            datetime(2026, 8, 8, 0, 1, tzinfo=ZoneInfo("Asia/Shanghai")),
        ]
    )
    client = FakeLLMClient(
        [
            FakeMessage("第一天", tool_calls=None),
            FakeMessage("第二天", tool_calls=None),
        ]
    )
    agent = AgentLoop(
        client,
        trace=None,
        timezone_name="Asia/Shanghai",
        clock=lambda: next(observed_times),
    )

    assert agent.run("日期？") == "第一天"
    assert agent.run("日期？") == "第二天"
    assert "当前日期是 2026-08-07" in client.requests[0][0]["content"]
    assert "当前日期是 2026-08-08" in client.requests[1][0]["content"]


def test_agent_rejects_unknown_timezone() -> None:
    with pytest.raises(ValueError, match="Unknown Agent timezone"):
        AgentLoop(
            FakeLLMClient([]),
            trace=None,
            timezone_name="Mars/Olympus_Mons",
        )


def test_agent_puts_tool_result_back_into_context() -> None:
    """验证工具结果以 role=tool 回填后，模型会收到第二轮上下文。"""
    client = FakeLLMClient(
        [
            FakeMessage(None, tool_calls=[make_tool_call()]),
            FakeMessage("文件讲解了 Agent Loop。", tool_calls=None),
        ]
    )
    agent = AgentLoop(client, trace=None)

    result = agent.run("总结 example.md")

    assert result == "文件讲解了 Agent Loop。"
    assert len(client.requests) == 2
    tool_message = client.requests[1][-1]
    assert tool_message["role"] == "tool"
    assert tool_message["tool_call_id"] == "call_1"
    assert '"success": true' in tool_message["content"]


def test_agent_stops_at_max_steps() -> None:
    """模型持续调用工具时，达到最大步数后必须停止。"""
    client = FakeLLMClient([FakeMessage(None, tool_calls=[make_tool_call()])])
    agent = AgentLoop(client, max_steps=1, trace=None)

    with pytest.raises(RuntimeError, match="1 步内完成"):
        agent.run("一直读取文件")


def test_agent_routes_run_code_and_returns_result_to_model() -> None:
    """模型调用 run_code 时，注册处理器结果应以 tool 消息回填。"""
    client = FakeLLMClient(
        [
            FakeMessage(
                None,
                tool_calls=[
                    make_tool_call("run_code", '{"code": "print(1 + 1)"}')
                ],
            ),
            FakeMessage("计算结果是 2。", tool_calls=None),
        ]
    )
    received_codes = []

    def fake_run_code(code: str) -> dict[str, Any]:
        received_codes.append(code)
        return {
            "success": True,
            "stdout": "2\n",
            "stderr": "",
            "sandbox_destroyed": True,
        }

    agent = AgentLoop(
        client,
        trace=None,
        tool_handlers={"run_code": fake_run_code},
    )
    result = agent.run("用 Python 计算 1 + 1")

    assert result == "计算结果是 2。"
    assert received_codes == ["print(1 + 1)"]
    assert client.tool_requests[0][0]["function"]["name"] == "run_code"
    tool_message = client.requests[1][-1]
    assert tool_message["role"] == "tool"
    assert '"sandbox_destroyed": true' in tool_message["content"]


def test_agent_rejects_unexpected_tool_arguments_before_handler() -> None:
    """运行时应落实 additionalProperties=false，而不是只相信模型。"""
    calls = []

    def fake_run_code(code: str) -> dict[str, Any]:
        calls.append(code)
        return {"success": True}

    agent = AgentLoop(
        FakeLLMClient([]),
        trace=None,
        tool_handlers={"run_code": fake_run_code},
    )
    result = agent._execute_tool(
        "run_code",
        '{"code": "print(1)", "dangerous": true}',
    )

    assert result["success"] is False
    assert result["error_type"] == "INVALID_ARGUMENTS"
    assert calls == []


@pytest.mark.parametrize(
    ("tool_name", "arguments", "expected_value"),
    [
        ("analyze_spreadsheet", '{"path":"fixtures/sales.csv"}', "fixtures/sales.csv"),
        (
            "generate_business_briefing",
            '{"path":"fixtures/operations.xlsx"}',
            "fixtures/operations.xlsx",
        ),
        ("web_search", '{"query":"Rust release notes"}', "Rust release notes"),
        ("web_fetch", '{"url":"https://example.com"}', "https://example.com"),
        ("browse_public_page", '{"url":"https://example.com"}', "https://example.com"),
    ],
)
def test_agent_routes_sales_and_browser_tools(
    tool_name: str,
    arguments: str,
    expected_value: str,
) -> None:
    received: list[str] = []

    def fake_handler(**kwargs) -> dict[str, Any]:
        received.extend(kwargs.values())
        return {"success": True, "sandbox_destroyed": True}

    agent = AgentLoop(
        FakeLLMClient([]),
        trace=None,
        tool_handlers={tool_name: fake_handler},
    )
    result = agent._execute_tool(tool_name, arguments)

    assert result["success"] is True
    assert received == [expected_value]
    assert agent.tools[0]["function"]["name"] == tool_name


def test_agent_can_search_then_fetch_before_answering() -> None:
    """验证当前 Agent 能连续完成 search → fetch → final answer。"""
    client = FakeLLMClient(
        [
            FakeMessage(
                None,
                tool_calls=[
                    make_tool_call("web_search", '{"query":"Rust release notes"}')
                ],
            ),
            FakeMessage(
                None,
                tool_calls=[
                    make_tool_call(
                        "web_fetch",
                        '{"url":"https://blog.rust-lang.org/releases/"}',
                    )
                ],
            ),
            FakeMessage("Rust 发布页已经核实。", tool_calls=None),
        ]
    )
    calls = []

    def fake_search(query: str) -> dict[str, Any]:
        calls.append(("search", query))
        return {
            "success": True,
            "results": [
                {
                    "title": "Rust Releases",
                    "url": "https://blog.rust-lang.org/releases/",
                }
            ],
            "trust": "untrusted",
            "instructions_executable": False,
        }

    def fake_fetch(url: str) -> dict[str, Any]:
        calls.append(("fetch", url))
        return {
            "success": True,
            "title": "Rust Releases",
            "text": "Verified release page",
            "trust": "untrusted",
            "instructions_executable": False,
        }

    agent = AgentLoop(
        client,
        trace=None,
        tool_handlers={"web_search": fake_search, "web_fetch": fake_fetch},
    )

    result = agent.run("搜索并核实 Rust 发布页")

    assert result == "Rust 发布页已经核实。"
    assert calls == [
        ("search", "Rust release notes"),
        ("fetch", "https://blog.rust-lang.org/releases/"),
    ]
    assert len(client.requests) == 3
    assert '"results"' in client.requests[1][-1]["content"]
    assert '"text": "Verified release page"' in client.requests[2][-1]["content"]


def test_agent_deduplicates_normalized_tool_calls_across_steps() -> None:
    """同一任务中的等价搜索只执行一次，并把第一次结果重新回填给模型。"""
    client = FakeLLMClient(
        [
            FakeMessage(
                None,
                tool_calls=[
                    make_tool_call(
                        "web_search",
                        '{"query":"Rust Release Notes"}',
                        call_id="search_1",
                    )
                ],
            ),
            FakeMessage(
                None,
                tool_calls=[
                    make_tool_call(
                        "web_search",
                        '{"query":"  rust   release notes  "}',
                        call_id="search_2",
                    )
                ],
            ),
            FakeMessage("使用已有搜索结果回答。", tool_calls=None),
        ]
    )
    received_queries: list[str] = []
    trace: list[str] = []

    def fake_search(query: str) -> dict[str, Any]:
        received_queries.append(query)
        return {"success": True, "result_count": 1, "results": []}

    agent = AgentLoop(
        client,
        trace=trace.append,
        tool_handlers={"web_search": fake_search},
    )

    assert agent.run("查 Rust 发布记录") == "使用已有搜索结果回答。"
    assert received_queries == ["Rust Release Notes"]
    duplicate_result = client.requests[2][-1]
    assert duplicate_result["tool_call_id"] == "search_2"
    duplicate_body = duplicate_result["content"]
    assert '"status": "deduplicated"' in duplicate_body
    assert '"budget_consumed": false' in duplicate_body
    assert any("工具去重命中（web_search" in item for item in trace)


def test_agent_enforces_unique_tool_call_budget_within_one_model_step() -> None:
    """模型一次返回多个调用也不能突破 web_search 的任务级硬预算。"""
    client = FakeLLMClient(
        [
            FakeMessage(
                None,
                tool_calls=[
                    make_tool_call(
                        "web_search",
                        '{"query":"Huawei latest phones"}',
                        call_id="search_1",
                    ),
                    make_tool_call(
                        "web_search",
                        '{"query":"Huawei official phones"}',
                        call_id="search_2",
                    ),
                    make_tool_call(
                        "web_search",
                        '{"query":"Huawei phone releases"}',
                        call_id="search_3",
                    ),
                ],
            ),
            FakeMessage("根据预算内的结果回答。", tool_calls=None),
        ]
    )
    received_queries: list[str] = []
    trace: list[str] = []

    def fake_search(query: str) -> dict[str, Any]:
        received_queries.append(query)
        return {"success": True, "result_count": 1, "results": []}

    agent = AgentLoop(
        client,
        trace=trace.append,
        tool_handlers={"web_search": fake_search},
    )

    assert agent.run("查询华为新手机") == "根据预算内的结果回答。"
    assert received_queries == [
        "Huawei latest phones",
        "Huawei official phones",
    ]
    budget_result = client.requests[1][-1]
    assert budget_result["tool_call_id"] == "search_3"
    budget_body = budget_result["content"]
    assert '"error_type": "TOOL_BUDGET_EXCEEDED"' in budget_body
    assert '"used": 2' in budget_body
    assert '"limit": 2' in budget_body
    assert any("工具预算已用尽（web_search，预算 2/2" in item for item in trace)


def test_agent_budget_is_configurable_and_exposed_to_model() -> None:
    client = FakeLLMClient([FakeMessage("直接回答", tool_calls=None)])
    agent = AgentLoop(
        client,
        trace=None,
        tool_handlers={"web_search": lambda query: {"success": True}},
        tool_budgets={"web_search": 1},
    )

    assert agent.run("你好") == "直接回答"
    assert "web_search 最多 1 次" in client.requests[0][0]["content"]

    with pytest.raises(ValueError, match="positive integer"):
        AgentLoop(
            FakeLLMClient([]),
            trace=None,
            tool_handlers={"web_search": lambda query: {"success": True}},
            tool_budgets={"web_search": 0},
        )
