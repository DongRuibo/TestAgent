"""14.1 目标版本、只读交接与独立的用户确认记录。"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Literal, Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from testagent_dev_agent.paths import REPOSITORY_ROOT


OPERATION_GOAL_DIR = REPOSITORY_ROOT / "artifacts" / "content_operations" / "goals"
GOAL_TOOL_NAMES = ("save_operation_goal", "load_operation_goal")
GOAL_SCENARIO = "operation_goal_confirmation"
MAX_GOAL_BYTES = 128_000
_GOAL_LOCK = threading.RLock()
Text = Annotated[str, Field(min_length=1, max_length=3000)]
ShortText = Annotated[str, Field(min_length=1, max_length=500)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class GoalPeriod(StrictModel):
    start: date
    end: date

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.end < self.start:
            raise ValueError("目标结束日期不能早于开始日期。")
        return self


class GoalMetric(StrictModel):
    name: ShortText
    definition: Text
    baseline: Text | None = None
    target: Text
    data_source: ShortText
    review_frequency: ShortText


class OperationGoal(StrictModel):
    summary: Text
    period: GoalPeriod
    platform: Literal["xiaohongshu"] = "xiaohongshu"
    business_direction: Text
    target_users: list[ShortText] = Field(min_length=1, max_length=20)
    metrics: list[GoalMetric] = Field(min_length=1, max_length=20)
    resources: list[ShortText] = Field(max_length=30)
    constraints: list[ShortText] = Field(max_length=30)
    # 这里只记录预算陈述，不把目标确认升级为预算审批。
    budget_statement: Text
    evidence: list[Text] = Field(max_length=30)
    assumptions: list[Text] = Field(max_length=20)
    risks: list[Text] = Field(max_length=20)


class SimpleWeeklyGoal(OperationGoal):
    metrics: list[GoalMetric] = Field(min_length=1, max_length=3)
    contribution: Text = Field(description="本周结果如何帮助月目标；结合实际进度与资源，不按整月除以四")


class GoalDraft(StrictModel):
    company_name: Annotated[str, Field(min_length=1, max_length=200)]
    company_profile: Annotated[str, Field(min_length=1, max_length=6000)]
    operation_goal: OperationGoal
    weekly_goal: SimpleWeeklyGoal | None = Field(default=None,
        description="月目标同时附带的一个简单周目标；与月目标同版保存、共同确认。单周期目标可省略。")

    @model_validator(mode="after")
    def weekly_within_month(self) -> Self:
        if self.weekly_goal:
            month, week = self.operation_goal.period, self.weekly_goal.period
            if not (month.start <= week.start <= week.end <= month.end):
                raise ValueError("简单周目标必须在月目标日期范围内。")
            if (week.end - week.start).days > 6:
                raise ValueError("简单周目标最多七天。")
            if (month.end - month.start).days <= 6:
                raise ValueError("组合目标的主目标须为较长周期，不能重复保存两个周目标。")
        return self


class GoalSaveRequest(GoalDraft):
    previous_goal_artifact_path: str | None = Field(default=None, max_length=2000)


class GoalDocument(GoalDraft):
    schema_version: Literal["1.0"] = "1.0"
    artifact_type: Literal["operation_goal"] = "operation_goal"
    goal_id: UUID
    version: int = Field(ge=1, strict=True)
    created_at: str
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class GoalReference(StrictModel):
    artifact_path: str
    goal_id: UUID
    version: int = Field(ge=1, strict=True)
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class GoalConfirmation(StrictModel):
    status: Literal["pending_confirmation", "confirmed_in_conversation"] = "pending_confirmation"
    source: Literal["web_user_action", "web_chat_message"] | None = None
    confirmed_at: str | None = None
    conversation_id: UUID | None = None
    ui_artifact_id: UUID | None = None
    idempotency_key: UUID | None = None
    user_turn_id: UUID | None = None

    @model_validator(mode="after")
    def consistent(self) -> Self:
        evidence = (self.source, self.confirmed_at, self.conversation_id,
                    self.ui_artifact_id, self.idempotency_key)
        if self.status == "confirmed_in_conversation" and not all(evidence):
            raise ValueError("目标确认缺少用户操作记录。")
        if self.status == "pending_confirmation" and any(evidence):
            raise ValueError("待确认目标不能携带确认记录。")
        if self.user_turn_id is not None and self.source != "web_chat_message":
            raise ValueError("聊天确认记录来源不一致。")
        if self.source == "web_chat_message" and self.user_turn_id is None:
            raise ValueError("聊天确认缺少用户消息记录。")
        return self


class GoalSnapshot(GoalDocument):
    confirmation: GoalConfirmation


class GoalConfirmationReceipt(StrictModel):
    goal_ref: GoalReference
    confirmation: GoalConfirmation


class GoalHandoff(StrictModel):
    goal_ref: GoalReference
    goal_snapshot: GoalSnapshot


def _hash(value: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()


def planning_goal(snapshot: dict[str, Any]) -> dict[str, Any]:
    """选择执行周目标，保留原月、周快照及其共同版本不变。"""
    return snapshot.get("weekly_goal") or snapshot["operation_goal"]


def _read_json(path: Path) -> Any:
    if path.is_symlink() or path.stat().st_size > MAX_GOAL_BYTES:
        raise ValueError("目标文件不能是符号链接或超过大小限制。")
    return json.loads(path.read_text("utf-8"))


def _write_json(path: Path, value: dict[str, Any]) -> None:
    content = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    if len(content.encode("utf-8")) > MAX_GOAL_BYTES:
        raise ValueError("目标文件超过大小限制。")
    # 完整写入后原子发布，且不覆盖已有版本；避免读到半个 JSON。
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent) as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())
        os.link(output.name, path)


class OperationGoalStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or OPERATION_GOAL_DIR).resolve()

    def _path(self, goal_id: UUID, version: int) -> Path:
        return self.root / str(goal_id) / f"v{version}" / "goal.json"

    def _resolve(self, path: str) -> Path:
        raw = Path(path).expanduser().absolute()
        resolved = raw.resolve()
        if not resolved.is_relative_to(self.root) or resolved.name != "goal.json":
            raise ValueError("goal_artifact_path 必须指向 14.1 目标目录中的 goal.json。")
        if any(item.is_symlink() for item in (raw, *raw.parents)):
            raise ValueError("目标路径不能包含符号链接。")
        return resolved

    def load(self, path: str, *, require_confirmed: bool = False,
             require_latest: bool = False) -> dict[str, Any]:
        with _GOAL_LOCK:
            resolved = self._resolve(path)
            document = GoalDocument.model_validate(_read_json(resolved))
            data = document.model_dump(mode="json")
            if data["weekly_goal"] is None:
                data.pop("weekly_goal")  # 兼容旧文件的原始哈希，不改写历史版本。
            if resolved != self._path(document.goal_id, document.version):
                raise ValueError("目标 ID、版本与文件路径不一致。")
            if _hash({key: value for key, value in data.items() if key != "content_hash"}) != document.content_hash:
                raise ValueError("目标内容哈希不一致，文件可能已被修改。")
            if require_latest and self._path(document.goal_id, document.version + 1).exists():
                raise ValueError("目标已有新版本，请显式选择并确认新版本。")
            reference = GoalReference(artifact_path=str(resolved), goal_id=document.goal_id,
                                      version=document.version, content_hash=document.content_hash)
            confirmation = GoalConfirmation()
            receipt_path = resolved.with_name("confirmation.json")
            if receipt_path.exists():
                receipt = GoalConfirmationReceipt.model_validate(_read_json(receipt_path)).model_dump(mode="json")
                if receipt.get("goal_ref") != reference.model_dump(mode="json"):
                    raise ValueError("目标确认记录与版本不一致。")
                confirmation = GoalConfirmation.model_validate(receipt["confirmation"])
            if require_confirmed and confirmation.status != "confirmed_in_conversation":
                raise ValueError("此目标版本尚未由用户在确认卡片上确认。")
            return GoalHandoff(
                goal_ref=reference,
                goal_snapshot=GoalSnapshot(**data, confirmation=confirmation),
            ).model_dump(mode="json")

    def save(self, request: GoalSaveRequest) -> dict[str, Any]:
        with _GOAL_LOCK:
            goal_id, version = uuid4(), 1
            if request.previous_goal_artifact_path is not None:
                previous = self.load(request.previous_goal_artifact_path, require_latest=True)
                old = previous["goal_snapshot"]
                if request.company_name != old["company_name"]:
                    raise ValueError("不能把其他企业的目标保存为此目标的新版本。")
                goal_id, version = UUID(old["goal_id"]), old["version"] + 1
            data = {
                **request.model_dump(mode="json", exclude={"previous_goal_artifact_path"}),
                "schema_version": "1.0", "artifact_type": "operation_goal",
                "goal_id": str(goal_id), "version": version,
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            if data["weekly_goal"] is None:
                data.pop("weekly_goal")
            data["content_hash"] = _hash(data)
            path = self._path(goal_id, version)
            self._resolve(str(path))
            path.parent.mkdir(parents=True, exist_ok=True)
            _write_json(path, data)
            return self.load(str(path))

    def confirm(self, reference: dict[str, Any], *, conversation_id: str,
                ui_artifact_id: str, idempotency_key: str, user_turn_id: str | None = None) -> dict[str, Any]:
        """仅供可信 Web 用户 Action 使用；不注册为模型工具。"""
        with _GOAL_LOCK:
            ref = GoalReference.model_validate(reference).model_dump(mode="json")
            handoff = self.load(ref["artifact_path"], require_latest=True)
            if handoff["goal_ref"] != ref:
                raise ValueError("确认卡片对应的目标版本或哈希已变化。")
            if handoff["goal_snapshot"]["confirmation"]["status"] == "pending_confirmation":
                confirmation = GoalConfirmation(
                    status="confirmed_in_conversation", source="web_chat_message" if user_turn_id else "web_user_action",
                    confirmed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    conversation_id=UUID(conversation_id), ui_artifact_id=UUID(ui_artifact_id),
                    idempotency_key=UUID(idempotency_key),
                    user_turn_id=UUID(user_turn_id) if user_turn_id else None,
                ).model_dump(mode="json")
                _write_json(Path(ref["artifact_path"]).with_name("confirmation.json"),
                            {"goal_ref": ref, "confirmation": confirmation})
            return self.load(ref["artifact_path"], require_confirmed=True)

    def save_tool(self, **payload: Any) -> dict[str, Any]:
        try:
            # 部分兼容接口把可选嵌套对象双重编码；只解码 JSON，仍完整校验。
            if isinstance(payload.get("weekly_goal"), str):
                try:
                    week = json.loads(payload["weekly_goal"])
                except json.JSONDecodeError:
                    raise ValueError("weekly_goal 必须是结构化 JSON 对象，不能是普通文字。") from None
                if not isinstance(week, dict):
                    raise ValueError("weekly_goal 必须是 JSON 对象；无需周目标时省略该字段。")
                payload = {**payload, "weekly_goal": week}
            handoff = self.save(GoalSaveRequest.model_validate(payload))
            return {"success": True, **handoff, "artifact_path": handoff["goal_ref"]["artifact_path"],
                    "message": "目标草案已保存，请在目标卡片上确认此版本。"}
        except (OSError, ValueError) as exc:
            return _tool_error(exc)

    def load_tool(self, goal_artifact_path: str) -> dict[str, Any]:
        try:
            return {"success": True, **self.load(goal_artifact_path, require_latest=True)}
        except (OSError, ValueError) as exc:
            return _tool_error(exc)

    def handlers(self) -> dict[str, Any]:
        return {"save_operation_goal": self.save_tool, "load_operation_goal": self.load_tool}


def _tool_error(exc: Exception) -> dict[str, Any]:
    message = ((".".join(map(str, exc.errors()[0]["loc"])) + ": " + exc.errors(include_input=False, include_url=False)[0]["msg"])
               if isinstance(exc, ValidationError) else str(exc))
    return {"success": False, "error_type": "INVALID_OPERATION_GOAL", "message": message}


_SAVE_SCHEMA = GoalSaveRequest.model_json_schema()
# 字段可省略，不需要 anyOf(object, null)，避免兼容接口误生成字符串。
_SAVE_SCHEMA["properties"]["weekly_goal"] = {
    "$ref": "#/$defs/SimpleWeeklyGoal",
    "description": "与月目标同时保存的简单周目标，必须传 JSON 对象而非字符串；单周期模式省略。",
}


GOAL_TOOL_DEFINITIONS = (
    {"type": "function", "function": {
        "name": "save_operation_goal",
        "description": "保存 14.1 小红书运营目标草案。修改时显式引用上一版本；确认状态由用户卡片操作记录，模型不能批准目标或预算。",
        "parameters": _SAVE_SCHEMA,
    }},
    {"type": "function", "function": {
        "name": "load_operation_goal",
        "description": "读取用户显式指定的 14.1 目标文件，返回企业、目标快照及用户确认记录。不得自行寻找或选择其他目标。",
        "parameters": {"type": "object", "properties": {
            "goal_artifact_path": {"type": "string", "maxLength": 2000},
        }, "required": ["goal_artifact_path"], "additionalProperties": False},
    }},
)
