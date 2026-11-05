"""命令行持续对话的离线测试。"""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import testagent_dev_agent.cli as cli_app
from main import run_interactive


class FakeInteractiveAgent:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[dict[str, str]], str | None]] = []

    def run(self, task: str, *, history=None, episode_id=None) -> str:
        self.calls.append((task, list(history or []), episode_id))
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

    first_episode_id = agent.calls[0][2]
    second_episode_id = agent.calls[2][2]
    assert first_episode_id is not None
    assert second_episode_id is not None
    assert first_episode_id != second_episode_id
    assert agent.calls == [
        ("第一个问题", [], first_episode_id),
        (
            "继续追问",
            [
                {"role": "user", "content": "第一个问题"},
                {"role": "assistant", "content": "回答：第一个问题"},
            ],
            first_episode_id,
        ),
        ("新话题", [], second_episode_id),
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


def test_cli_merges_namespaced_mcp_tool(monkeypatch, capsys) -> None:
    tool_name = "mcp__lazytt__area_calculate"
    definition = {
        "type": "function",
        "function": {
            "name": tool_name,
            "description": "远程面积计算。",
            "parameters": {
                "type": "object",
                "properties": {"rooms": {"type": "array"}},
                "required": ["rooms"],
                "additionalProperties": False,
            },
        },
    }
    captured = {}

    class FakeAgent:
        def __init__(self, _client, **kwargs):
            captured.update(kwargs)

        def run(self, task, **_kwargs):
            assert task == "计算面积"
            return "完成"

    monkeypatch.setattr(cli_app, "parse_args", lambda: Namespace(task="计算面积"))
    monkeypatch.setattr(
        cli_app.LLMClient,
        "from_env",
        classmethod(lambda cls: object()),
    )
    monkeypatch.setattr(cli_app, "build_memory_client_from_env", lambda: None)
    monkeypatch.setattr(
        cli_app,
        "build_lazytt_mcp_tools_from_env",
        lambda: SimpleNamespace(
            tool_definitions={tool_name: definition},
            tool_handlers={tool_name: lambda rooms: {"success": True}},
            tool_budgets={tool_name: 1},
        ),
    )
    monkeypatch.setattr(cli_app, "AgentLoop", FakeAgent)

    cli_app.main()

    assert tool_name in captured["tool_definitions"]
    assert tool_name in captured["tool_handlers"]
    assert "calculate_material_quantities" not in captured["tool_definitions"]
    assert "calculate_material_quantities" not in captured["tool_handlers"]
    assert captured["tool_budgets"] == {tool_name: 1}
    assert capsys.readouterr().out.strip() == "Assistant: 完成"


def test_cli_lists_skills_without_initializing_model(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    skill_dir = tmp_path / ".agents" / "skills" / "simple-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: simple-skill
description: 一个普通的本地 Skill。
metadata:
  version: "1.2.3"
---

# 普通 Skill

按用户要求回答。
""",
        "utf-8",
    )
    monkeypatch.setattr(cli_app, "REPOSITORY_ROOT", tmp_path)
    monkeypatch.setattr(
        cli_app,
        "parse_args",
        lambda: Namespace(task=None, skill=None, list_skills=True),
    )
    monkeypatch.setattr(
        cli_app.LLMClient,
        "from_env",
        classmethod(lambda cls: (_ for _ in ()).throw(AssertionError("not called"))),
    )

    cli_app.main()

    assert capsys.readouterr().out.strip() == (
        "simple-skill\t1.2.3\t一个普通的本地 Skill。"
    )
