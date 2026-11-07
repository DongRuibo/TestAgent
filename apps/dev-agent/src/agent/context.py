"""单 Agent 的最小上下文预算、记忆召回策略和回执。"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any, Literal


DEFAULT_CONTEXT_MAX_CHARS = 40_000
DEFAULT_HISTORY_MAX_CHARS = 16_000
DEFAULT_TOOL_RESULT_MAX_CHARS = 8_000


@dataclass(frozen=True)
class ContextConfig:
    context_max_chars: int = DEFAULT_CONTEXT_MAX_CHARS
    history_max_chars: int = DEFAULT_HISTORY_MAX_CHARS
    tool_result_max_chars: int = DEFAULT_TOOL_RESULT_MAX_CHARS

    @classmethod
    def from_env(cls) -> "ContextConfig":
        context_max_chars = _bounded_int_env(
            "CONTEXT_MAX_CHARS",
            DEFAULT_CONTEXT_MAX_CHARS,
            minimum=4_000,
            maximum=1_000_000,
        )
        history_max_chars = _bounded_int_env(
            "CONTEXT_HISTORY_MAX_CHARS",
            DEFAULT_HISTORY_MAX_CHARS,
            minimum=0,
            maximum=context_max_chars,
        )
        tool_result_max_chars = _bounded_int_env(
            "CONTEXT_TOOL_RESULT_MAX_CHARS",
            DEFAULT_TOOL_RESULT_MAX_CHARS,
            minimum=1_000,
            maximum=context_max_chars,
        )
        return cls(context_max_chars, history_max_chars, tool_result_max_chars)


@dataclass(frozen=True)
class RecallDecision:
    action: Literal["recall", "skip"]
    reason: str


class MemoryRecallPolicy:
    """用少量确定性规则跳过明显不需要长期记忆的任务。"""

    def __init__(self, mode: Literal["adaptive", "always", "off"] = "adaptive") -> None:
        if mode not in {"adaptive", "always", "off"}:
            raise ValueError("Memory recall policy must be adaptive, always or off")
        self.mode = mode

    @classmethod
    def from_env(cls) -> "MemoryRecallPolicy":
        raw = os.getenv("MEMORY_RECALL_POLICY", "adaptive").strip().lower()
        if raw not in {"adaptive", "always", "off"}:
            raise ValueError("MEMORY_RECALL_POLICY 必须是 adaptive、always 或 off。")
        return cls(raw)

    def decide(self, task: str) -> RecallDecision:
        normalized = " ".join(task.casefold().split())
        if self.mode == "always":
            return RecallDecision("recall", "配置为每轮召回")
        if self.mode == "off":
            return RecallDecision("skip", "配置为不召回长期记忆")
        if any(marker in normalized for marker in ("不要使用记忆", "不需要记忆", "ignore memory")):
            return RecallDecision("skip", "用户明确要求不使用长期记忆")

        continuity_markers = (
            "记得",
            "之前",
            "上次",
            "以前",
            "继续",
            "沿用",
            "刚才",
            "remember",
            "previous",
            "last time",
            "earlier",
            "continue",
            "reuse",
        )
        material_markers = ("油漆", "瓷砖", "地板", "paint", "tile", "flooring")
        calculation_markers = (
            "计算",
            "采购量",
            "用量",
            "估算",
            "calculate",
            "quantity",
            "estimate",
        )
        if (
            any(marker in normalized for marker in material_markers)
            and any(marker in normalized for marker in calculation_markers)
            and not any(marker in normalized for marker in continuity_markers)
        ):
            return RecallDecision("skip", "材料精确计算必须使用本轮输入")

        memory_markers = (
            "记得",
            "之前",
            "上次",
            "以前",
            "偏好",
            "习惯",
            "项目",
            "继续",
            "remember",
            "previous",
            "last time",
            "preference",
            "earlier",
        )
        if any(marker in normalized for marker in memory_markers):
            return RecallDecision("recall", "任务包含历史、偏好或项目连续性信号")

        if re.search(r"\bpython\b", normalized) and any(
            marker in normalized
            for marker in ("计算", "运行", "执行", "代码", "精确", "calculate", "run", "execute")
        ):
            return RecallDecision("skip", "明确的 Python 确定性任务")
        if re.search(r"\d\s*[+\-*/×÷]\s*\d", normalized) and any(
            marker in normalized for marker in ("计算", "结果", "等于", "calculate", "result")
        ):
            return RecallDecision("skip", "纯算术任务")
        if re.search(r"https?://", normalized) and any(
            marker in normalized for marker in ("打开", "访问", "读取", "总结", "open", "read", "fetch")
        ):
            return RecallDecision("skip", "指定 URL 的确定性读取任务")
        if re.search(r"\.(?:txt|md|csv|xlsx|json|py|pdf)\b", normalized) and any(
            marker in normalized for marker in ("读取", "打开", "总结", "分析", "read", "open", "summarize")
        ):
            return RecallDecision("skip", "指定文件的确定性处理任务")
        if any(marker in normalized for marker in ("格式转换", "转换格式", "format conversion")):
            return RecallDecision("skip", "确定性格式转换任务")
        return RecallDecision("recall", "普通任务默认召回，避免漏掉相关长期记忆")


@dataclass(frozen=True)
class HistorySelection:
    messages: tuple[dict[str, str], ...]
    omitted_count: int
    estimated_chars: int


def select_completed_history(
    history: Sequence[Mapping[str, str]],
    *,
    max_chars: int,
) -> HistorySelection:
    """从新到旧选择完整 user/assistant 轮次，不拆开一轮。"""
    messages = [dict(item) for item in history]
    if len(messages) % 2 != 0:
        raise ValueError("history 必须由完整的 user/assistant 轮次组成。")
    turns: list[list[dict[str, str]]] = []
    for index in range(0, len(messages), 2):
        turn = messages[index : index + 2]
        if turn[0].get("role") != "user" or turn[1].get("role") != "assistant":
            raise ValueError("history 必须按 user/assistant 顺序排列。")
        turns.append(turn)

    selected_turns: list[list[dict[str, str]]] = []
    used = 0
    for turn in reversed(turns):
        turn_chars = sum(len(item["content"]) for item in turn)
        if used + turn_chars > max_chars:
            break
        selected_turns.append(turn)
        used += turn_chars
    selected_turns.reverse()
    selected = tuple(item for turn in selected_turns for item in turn)
    return HistorySelection(selected, len(messages) - len(selected), used)


@dataclass
class ContextReceipt:
    task_id: str
    attempt_id: str
    history_message_count: int = 0
    history_omitted_count: int = 0
    memory_policy: str = "disabled"
    memory_reason: str = "Memory integration is disabled"
    memory_item_count: int = 0
    memory_context_pack_id: str | None = None
    memory_record_versions: tuple[dict[str, str], ...] = ()
    tool_result_count: int = 0
    estimated_context_chars: int = 0
    context_estimation_method: str = "serialized_chars_not_tokens"
    budget_checks: list[dict[str, Any]] = field(default_factory=list)
    model_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    completion: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def estimate_messages_chars(messages: Sequence[Mapping[str, Any]]) -> int:
    """估算消息序列化后的字符数，包含工具参数和协议字段，并非精确 token 数。"""
    return len(json.dumps(list(messages), ensure_ascii=False, separators=(",", ":")))


def serialize_tool_result(result: dict[str, Any], *, max_chars: int) -> str:
    serialized = json.dumps(result, ensure_ascii=False)
    if len(serialized) <= max_chars:
        return serialized
    compact = {
        "truncated": True,
        "original_chars": len(serialized),
        "preview": "",
        "message": "工具结果过长，当前上下文只保留预览；完整文件应通过 Artifact 路径读取。",
    }
    overhead = len(json.dumps(compact, ensure_ascii=False))
    compact["preview"] = serialized[: max(0, max_chars - overhead)]
    encoded = json.dumps(compact, ensure_ascii=False)
    if len(encoded) > max_chars:
        overflow = len(encoded) - max_chars
        compact["preview"] = compact["preview"][:-overflow]
        encoded = json.dumps(compact, ensure_ascii=False)
    return encoded


def _bounded_int_env(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} 必须是整数。") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} 必须在 {minimum} 到 {maximum} 之间。")
    return value


__all__ = [
    "ContextConfig",
    "ContextReceipt",
    "HistorySelection",
    "MemoryRecallPolicy",
    "RecallDecision",
    "estimate_messages_chars",
    "select_completed_history",
    "serialize_tool_result",
]
