from __future__ import annotations

import json

import pytest

from agent.context import (
    MemoryRecallPolicy,
    select_completed_history,
    serialize_tool_result,
)


def test_adaptive_memory_policy_skips_explicit_python_calculation() -> None:
    decision = MemoryRecallPolicy("adaptive").decide(
        "请调用 Python 精确计算 1234567 × 7654321，并告诉我结果。"
    )

    assert decision.action == "skip"
    assert "Python" in decision.reason


def test_adaptive_memory_policy_recalls_project_history_before_tool_rules() -> None:
    decision = MemoryRecallPolicy("adaptive").decide(
        "请用 Python 继续分析我之前的项目数据。"
    )

    assert decision.action == "recall"
    assert "项目" in decision.reason


def test_adaptive_memory_policy_skips_material_calculation_without_continuity() -> None:
    decision = MemoryRecallPolicy("adaptive").decide(
        "计算油漆、瓷砖、地板采购量并用界面汇总"
    )

    assert decision.action == "skip"
    assert "本轮输入" in decision.reason


def test_adaptive_memory_policy_recalls_explicit_previous_material_parameters() -> None:
    decision = MemoryRecallPolicy("adaptive").decide(
        "沿用上次参数，重新计算油漆、瓷砖和地板采购量"
    )

    assert decision.action == "recall"
    assert "历史" in decision.reason


def test_history_selection_keeps_only_complete_recent_turns() -> None:
    history = [
        {"role": "user", "content": "旧问题"},
        {"role": "assistant", "content": "旧回答"},
        {"role": "user", "content": "新问题"},
        {"role": "assistant", "content": "新回答"},
    ]

    selected = select_completed_history(history, max_chars=6)

    assert selected.messages == (
        {"role": "user", "content": "新问题"},
        {"role": "assistant", "content": "新回答"},
    )
    assert selected.omitted_count == 2


def test_history_selection_rejects_incomplete_turn() -> None:
    with pytest.raises(ValueError, match="完整"):
        select_completed_history(
            [{"role": "user", "content": "还没有回答"}],
            max_chars=100,
        )


def test_long_tool_result_becomes_valid_json_preview() -> None:
    serialized = serialize_tool_result({"content": "x" * 2000}, max_chars=1000)
    payload = json.loads(serialized)

    assert payload["truncated"] is True
    assert payload["original_chars"] > 1000
    assert len(serialized) <= 1000
    assert payload["preview"]
