"""保存 14.2 网络研究与候选选题的轻量结构化工件。"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Literal, Self
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from testagent_dev_agent.paths import REPOSITORY_ROOT
from tools.operation_goals import GoalHandoff, OperationGoalStore, planning_goal
from tools.research_images import VisualReceipt


SAVE_CONTENT_RESEARCH_TOOL_NAME = "save_content_research_result"
CONTENT_RESEARCH_OUTPUT_DIR = (
    REPOSITORY_ROOT / "artifacts" / "content_operations" / "research"
)
MAX_ARTIFACT_BYTES = 2_000_000
RESEARCH_SAVE_ATTEMPTS = 3


def safe_research_draft(payload: dict[str, Any]) -> dict[str, Any]:
    """仅为本地恢复保留有界 JSON；不保存已配置凭据或认证字段。"""
    secret_key = re.compile(r"api[_-]?(?:key|token)|password|secret|authorization|cookie", re.I)
    secrets = [value for key, value in os.environ.items()
        if len(value) >= 8 and (secret_key.search(key) or key.upper().endswith("_TOKEN"))]

    def clean(value):
        if isinstance(value, dict):
            return {key: "<REDACTED>" if secret_key.search(key) else clean(item) for key, item in value.items()}
        if isinstance(value, list):
            return [clean(item) for item in value]
        if isinstance(value, str):
            for secret in secrets:
                value = value.replace(secret, "<REDACTED>")
            value = re.sub(r"(?i)(Bearer\s+)[\w.\-]+", r"\1<REDACTED>", value)
            return re.sub(r"(?i)((?:api[_-]?(?:key|token)|password|secret|authorization)\s*[:=]\s*)[^\s,;\"&]+", r"\1<REDACTED>", value)
        return value

    serialized = json.dumps(clean(payload), ensure_ascii=False, allow_nan=False)
    if len(serialized.encode("utf-8")) > MAX_ARTIFACT_BYTES:
        raise ValueError("研究草稿超过 2 MB，未保留新草稿；请缩减文本后保存。")
    return json.loads(serialized)

ShortText = Annotated[str, Field(min_length=1, max_length=500)]
Text = Annotated[str, Field(min_length=1, max_length=1000)]
SourceId = Annotated[str, Field(min_length=1, max_length=100)]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ResearchFinding(_StrictModel):
    statement: Text
    kind: Literal["fact", "inference"]
    source_refs: list[SourceId] = Field(max_length=10)
    uncertainty: ShortText
    suggested_action: ShortText

    @model_validator(mode="after")
    def fact_requires_source(self) -> Self:
        if self.kind == "fact" and not self.source_refs:
            raise ValueError("事实必须引用来源。")
        return self


class SourceReference(_StrictModel):
    source_id: SourceId
    platform: Literal["xiaohongshu"]
    post_id: Annotated[str, Field(max_length=200)]
    url: Annotated[str, Field(min_length=1, max_length=2000)]
    keyword: Annotated[str, Field(min_length=1, max_length=80)]
    collected_at: Annotated[str, Field(min_length=1, max_length=64)]
    verification: Literal["legacy_unverified", "search_only", "body_read", "comments_read", "visual_read"] = "legacy_unverified"
    receipt_ids: list[SourceId] = Field(default_factory=list, max_length=10)

    @field_validator("url")
    @classmethod
    def xiaohongshu_https_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        hostname = (parsed.hostname or "").rstrip(".").casefold()
        if (
            parsed.scheme.casefold() != "https"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
            or not (
                hostname == "xiaohongshu.com"
                or hostname.endswith(".xiaohongshu.com")
            )
        ):
            raise ValueError("必须是公开小红书 HTTPS URL。")
        return value

    @field_validator("collected_at")
    @classmethod
    def timezone_aware_time(cls, value: str) -> str:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("必须是 ISO 8601 时间。") from exc
        if parsed.tzinfo is None:
            raise ValueError("必须包含时区。")
        return value


class TopicEvidence(_StrictModel):
    fact: Text
    source_refs: list[SourceId] = Field(min_length=1, max_length=10)
    kind: Literal["fact", "inference"] = "fact"
    uncertainty: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def inference_requires_uncertainty(self):
        if self.kind == "inference" and not self.uncertainty:
            raise ValueError("推测依据需说明不确定性。")
        return self


class TopicRating(_StrictModel):
    level: int = Field(ge=1, le=5)
    reason: Annotated[str, Field(min_length=1, max_length=300)]


class TopicRatings(_StrictModel):
    trend: TopicRating
    demand: TopicRating
    business_fit: TopicRating
    commercial_value: TopicRating
    competition: TopicRating


class CandidateTopic(_StrictModel):
    title: Annotated[str, Field(min_length=1, max_length=200)]
    evidence: list[TopicEvidence] = Field(min_length=1, max_length=10)
    why_fit_company: Text
    target_user: ShortText
    recommended_platform: list[Literal["xiaohongshu"]] = Field(
        min_length=1,
        max_length=1,
    )
    expected_result: list[Text] = Field(min_length=1, max_length=5)
    required_materials: list[Text] = Field(min_length=1, max_length=10)
    risks: list[Text] = Field(min_length=1, max_length=10)
    ratings: TopicRatings
    recommendation: Literal["high", "medium", "low"]
    visual_refs: list[SourceId] = Field(default_factory=list, max_length=2)
    visual_borrowing: str = Field(default="", max_length=500)
    planned_photo_count: int | None = Field(default=None, ge=0, le=100,
        description="本选题实际需要的现有照片总数；宿主给出照片上限时必填，不能超过上限。数量不代表已核实具体画面。")


class ContentResearchPayload(_StrictModel):
    company_name: Annotated[str, Field(min_length=1, max_length=200)]
    company_profile: Annotated[str, Field(min_length=1, max_length=6000)]
    operation_goal: Annotated[str, Field(min_length=1, max_length=3000)]
    queries: list[Annotated[str, Field(min_length=1, max_length=80)]] = Field(
        min_length=1,
        max_length=12,
    )
    research_summary: Annotated[str, Field(min_length=1, max_length=3000)]
    key_findings: list[ResearchFinding] = Field(max_length=30)
    user_needs: list[ResearchFinding] = Field(max_length=30)
    competitor_findings: list[ResearchFinding] = Field(max_length=30)
    platform_findings: list[ResearchFinding] = Field(max_length=30)
    source_references: list[SourceReference] = Field(max_length=100)
    candidate_topics: list[CandidateTopic] = Field(max_length=10)
    execution_status: Literal["success", "partial_success", "failed"]
    limitations: list[Text] = Field(max_length=20)
    visual_analyses: list[VisualReceipt] = Field(default_factory=list, max_length=2)

    @model_validator(mode="after")
    def validate_evidence_and_status(self) -> Self:
        visual_ids = [item.analysis_id for item in self.visual_analyses]
        if len(set(visual_ids)) != len(visual_ids):
            raise ValueError("图片分析编号不能重复。")
        available = {item.analysis_id for item in self.visual_analyses if item.status == "success" and item.analysis}
        visual_posts = {item.analysis_id: urlsplit(item.post_url).path.rstrip("/") for item in self.visual_analyses}
        source_posts = {item.source_id: urlsplit(item.url).path.rstrip("/") for item in self.source_references}
        for topic in self.candidate_topics:
            if set(topic.visual_refs) - available or (topic.visual_borrowing and not topic.visual_refs):
                raise ValueError("视觉借鉴必须引用成功的图片分析。")
            referenced_posts = {source_posts.get(ref) for item in topic.evidence for ref in item.source_refs}
            if any(visual_posts[ref] not in referenced_posts for ref in topic.visual_refs):
                raise ValueError("视觉依据必须同时关联相应笔记来源，不能借用其他笔记的分析。")
        source_ids = [item.source_id for item in self.source_references]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("source_id 不能重复。")
        referenced = {
            source_id
            for group in (
                self.key_findings,
                self.user_needs,
                self.competitor_findings,
                self.platform_findings,
            )
            for finding in group
            for source_id in finding.source_refs
        }
        referenced.update(
            source_id
            for topic in self.candidate_topics
            for evidence in topic.evidence
            for source_id in evidence.source_refs
        )
        unknown = sorted(referenced - set(source_ids))
        if unknown:
            raise ValueError(f"引用了未知来源：{', '.join(unknown)}")
        if self.execution_status in {"success", "partial_success"}:
            if not self.source_references:
                raise ValueError("成功或部分成功的研究必须包含来源。")
            if not 1 <= len(self.candidate_topics) <= 10:
                raise ValueError("成功或部分成功的研究必须包含 1-10 个有依据的候选选题。")
        elif self.candidate_topics:
            raise ValueError("失败的研究不能生成候选选题。")
        return self


class ContentResearchSaveRequest(ContentResearchPayload):
    company_name: str | None = None
    company_profile: str | None = None
    operation_goal: str | None = None
    goal_artifact_path: str | None = Field(default=None, max_length=2000)


SAVE_CONTENT_RESEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": SAVE_CONTENT_RESEARCH_TOOL_NAME,
        "description": (
            "校验并保存 14.2 小红书网络研究结果。只保存候选选题，审批状态固定为待确认，"
            "不发布、不投流。"
        ),
        "parameters": ContentResearchSaveRequest.model_json_schema(),
    },
}
# 回执由宿主从真实工具结果注入，不要求研究模型复制视觉记录。
SAVE_CONTENT_RESEARCH_TOOL["function"]["parameters"]["properties"].pop("visual_analyses")
for _receipt_type in ("VisualReceipt", "ImageAnalysis", "ImageObservation"):
    SAVE_CONTENT_RESEARCH_TOOL["function"]["parameters"]["$defs"].pop(_receipt_type, None)


class ContentResearchTools:
    def __init__(
        self,
        *,
        output_dir: Path = CONTENT_RESEARCH_OUTPUT_DIR,
        clock: Callable[[], datetime] | None = None,
        goal_store: OperationGoalStore | None = None,
    ) -> None:
        self.output_dir = output_dir
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.goal_store = goal_store or OperationGoalStore()

    def save_result(self, **result: Any) -> dict[str, Any]:
        try:
            request = ContentResearchSaveRequest.model_validate(result)
            raw = request.model_dump(mode="json", exclude={"goal_artifact_path"})
            handoff = {"goal_ref": None, "goal_snapshot": None}
            if request.goal_artifact_path is not None:
                handoff = self.goal_store.load(
                    request.goal_artifact_path, require_confirmed=True, require_latest=True
                )
                snapshot = handoff["goal_snapshot"]
                expected = {
                    "company_name": snapshot["company_name"],
                    "company_profile": snapshot["company_profile"],
                    "operation_goal": planning_goal(snapshot)["summary"],
                }
                for name, value in expected.items():
                    if raw[name] is not None and raw[name] != value:
                        raise ValueError(f"{name} 与显式引用的目标版本不一致。")
                    raw[name] = value
            normalized = _validated_result(raw)
            artifact = {
                "schema_version": "1.1",
                "artifact_type": "content_research",
                "research_id": str(uuid4()),
                "version": 1,
                "research_time": self._research_time(),
                "platforms": ["xiaohongshu"],
                "approval_status": "pending_confirmation",
                "input_mode": "goal_artifact" if request.goal_artifact_path else "explicit_text",
                **handoff,
                **normalized,
            }
            path = self.output_dir / artifact["research_id"] / "research.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(artifact, ensure_ascii=False, indent=2) + "\n",
                "utf-8",
            )
        except (OSError, TypeError, ValueError) as exc:
            message = str(exc)
            errors = []
            if isinstance(exc, ValidationError):
                errors = [{"field": ".".join(str(item) for item in error["loc"]) or "root",
                    "type": error["type"], "message": error["msg"]}
                    for error in exc.errors(include_url=False, include_input=False, include_context=False)[:20]]
                message = f"研究输入字段 {errors[0]['field']} 无效：{errors[0]['message']}"
            if isinstance(exc, OSError):
                message = "研究文件写入失败，请检查本地磁盘与目录权限。"
            return {
                "success": False,
                "error_type": "CONTENT_RESEARCH_WRITE_FAILED" if isinstance(exc, OSError) else "INVALID_CONTENT_RESEARCH_RESULT",
                **safe_research_draft({"message": message, "validation_errors": errors}),
                "retryable": True,
                "retry_scope": "local_save_only",
            }
        return {
            "success": True,
            "workflow": "content_research_v0",
            "research_id": artifact["research_id"],
            "execution_status": artifact["execution_status"],
            "approval_status": artifact["approval_status"],
            "candidate_topic_count": len(artifact["candidate_topics"]),
            "artifact_path": str(path),
            "message": "14.2 研究结果已保存，候选选题等待负责人确认。",
        }

    def handlers(self) -> dict[str, Callable[..., dict[str, Any]]]:
        return {SAVE_CONTENT_RESEARCH_TOOL_NAME: self.save_result}

    def _research_time(self) -> str:
        value = self.clock()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def load_content_research_artifact(path: str) -> dict[str, Any]:
    """为 14.3 读取并重新校验 14.2 工件，拒绝任意路径。"""
    raw_path = Path(path).expanduser()
    requested = raw_path.resolve()
    root = CONTENT_RESEARCH_OUTPUT_DIR.resolve()
    if (
        raw_path.is_symlink()
        or requested.suffix != ".json"
        or not requested.is_relative_to(root)
    ):
        raise ValueError("research_artifact_path 必须指向 14.2 工件目录内的 JSON。")
    if requested.stat().st_size > MAX_ARTIFACT_BYTES:
        raise ValueError("14.2 研究工件超过大小限制。")
    payload = json.loads(requested.read_text("utf-8"))
    if not isinstance(payload, dict) or payload.get("artifact_type") != "content_research":
        raise ValueError("不是合法的 14.2 研究工件。")
    schema_version = payload.get("schema_version")
    if schema_version not in {"1.0", "1.1"}:
        raise ValueError("不支持的 14.2 工件版本。")
    try:
        UUID(str(payload.get("research_id", "")))
    except ValueError as exc:
        raise ValueError("14.2 研究工件缺少合法 research_id。") from exc
    fields = ContentResearchPayload.model_fields
    validated = _validated_result({name: payload[name] for name in fields if name in payload})
    if schema_version == "1.0":
        if payload.get("goal_ref") is not None or payload.get("goal_snapshot") is not None:
            raise ValueError("旧版研究不能声明未经校验的目标来源。")
        lineage = {"input_mode": "legacy", "goal_ref": None, "goal_snapshot": None, "version": 1}
    else:
        lineage = {name: payload.get(name) for name in ("input_mode", "goal_ref", "goal_snapshot", "version")}
        if lineage["version"] != 1 or type(lineage["version"]) is not int:
            raise ValueError("研究版本无效。")
        if lineage["input_mode"] == "goal_artifact":
            saved = GoalHandoff.model_validate({name: lineage[name] for name in ("goal_ref", "goal_snapshot")}).model_dump(mode="json")
            original = OperationGoalStore().load(saved["goal_ref"]["artifact_path"], require_confirmed=True)
            if original != saved:
                raise ValueError("研究目标快照、引用或确认记录与来源文件不一致。")
            snapshot = saved["goal_snapshot"]
            if (validated["company_name"] != snapshot["company_name"]
                    or validated["company_profile"] != snapshot["company_profile"]
                    or validated["operation_goal"] != planning_goal(snapshot)["summary"]):
                raise ValueError("研究企业或目标与目标快照不一致。")
        elif (lineage["input_mode"] != "explicit_text"
              or lineage["goal_ref"] is not None or lineage["goal_snapshot"] is not None):
            raise ValueError("研究输入模式或目标来源无效。")
    return {**payload, **validated, **lineage}


def _validated_result(result: dict[str, Any]) -> dict[str, Any]:
    try:
        return ContentResearchPayload.model_validate(result).model_dump(mode="json")
    except ValidationError as exc:
        first = exc.errors(include_url=False, include_input=False)[0]
        field = ".".join(str(item) for item in first["loc"]) or "root"
        raise ValueError(f"研究结果字段 {field} 无效：{first['msg']}") from exc


__all__ = [
    "CONTENT_RESEARCH_OUTPUT_DIR",
    "SAVE_CONTENT_RESEARCH_TOOL",
    "SAVE_CONTENT_RESEARCH_TOOL_NAME",
    "ContentResearchTools",
    "load_content_research_artifact",
]
