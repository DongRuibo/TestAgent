"""命令行持续对话的离线测试。"""

from __future__ import annotations

from main import run_interactive


class FakeInteractiveAgent:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[dict[str, str]]]] = []

    def run(self, task: str, *, history=None) -> str:
        self.calls.append((task, list(history or [])))
        return f"回答：{task}"


def test_interactive_main_keeps_history_and_clear_resets_it() -> None:
    inputs = iter(["第一个问题", "继续追问", "/clear", "新话题", "/exit"])
    outputs: list[str] = []
    agent = FakeInteractiveAgent()

    run_interactive(
        agent,
        input_fn=lambda _prompt: next(inputs),
        output_fn=outputs.append,
    )

    assert agent.calls == [
        ("第一个问题", []),
        (
            "继续追问",
            [
                {"role": "user", "content": "第一个问题"},
                {"role": "assistant", "content": "回答：第一个问题"},
            ],
        ),
        ("新话题", []),
    ]
    assert "上下文已清空。" in outputs
    assert outputs[-1] == "对话已结束。"


def test_interactive_main_skips_blank_input() -> None:
    inputs = iter(["   ", "/quit"])
    agent = FakeInteractiveAgent()

    run_interactive(
        agent,
        input_fn=lambda _prompt: next(inputs),
        output_fn=lambda _message: None,
    )

    assert agent.calls == []
