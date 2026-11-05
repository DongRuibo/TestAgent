"""单条图文的结构化候选与受控展示；审批和执行由宿主管理。"""

from copy import deepcopy
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from tools.content_operations_ui import _spec


DRAFT_SCENARIO = "content_draft"
SAVE_DRAFT_TOOL = "save_content_draft_candidate"


class DraftClaim(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    text: str = Field(min_length=1, max_length=1500)
    source_refs: list[str] = Field(min_length=1, max_length=10)


class DraftPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=500)
    body: str = Field(min_length=1, max_length=30000)
    tags: list[str] = Field(default_factory=list, max_length=30)
    image_notes: str = Field(min_length=1, max_length=3000)
    source_refs: list[str] = Field(min_length=1, max_length=20)
    claims: list[DraftClaim] = Field(default_factory=list, max_length=30)
    missing_materials: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("title", "body", "image_notes")
    @classmethod
    def required_text(cls, value):
        if not value:
            raise ValueError("标题、正文和图片说明不能为空。")
        return value

    @field_validator("tags")
    @classmethod
    def tags_are_bounded(cls, values):
        tags = [value.lstrip("#").strip() for value in values]
        if any(not value or len(value) > 50 for value in tags) or len(set(tags)) != len(tags):
            raise ValueError("标签须非空、不重复且每条不超过50字。")
        return tags


DRAFT_TOOL_DEFINITION = {"type": "function", "function": {"name": SAVE_DRAFT_TOOL,
    "description": "保存本条图文候选。事实须列出claims及来源；仅可引用本次输入来源。保存不代表审核或执行。",
    "parameters": DraftPayload.model_json_schema()}}


def build_content_draft_ui(data, artifact_id=None):
    actions = []
    shared = {"result_ref": {"type": "string", "minLength": 1},
        "version": {"type": "integer", "minimum": 1}}
    for action, label, extra, required in (
        ("edit_content_draft", "保存修改为新版本", {
            "title": {"type": "string", "minLength": 1, "maxLength": 500},
            "body": {"type": "string", "minLength": 1, "maxLength": 30000, "format": "preserve_newlines"},
            "tags_text": {"type": "string", "maxLength": 2000, "format": "multiline"},
            "image_paths_text": {"type": "string", "maxLength": 12000, "format": "multiline"},
            "materials_note": {"type": "string", "maxLength": 2000}}, ["title", "body"]),
        ("review_content_draft", "保存本版本审核", {
            "decision": {"type": "string", "enum": ["approve", "revise"]},
            "note": {"type": "string", "maxLength": 2000}}, ["decision"]),
        ("regenerate_content_draft", "只重新生成此条", {"note": {"type": "string", "minLength": 1, "maxLength": 2000}}, ["note"]),
        ("save_content_draft", "确认保存小红书草稿", {}, []),
        ("reconcile_content_draft", "查询原执行状态", {}, []),
        ("resolve_content_draft", "保存人工核验结论", {
            "decision": {"type": "string", "enum": ["found", "not_saved"]},
            "note": {"type": "string", "minLength": 1, "maxLength": 2000}}, ["decision", "note"]),
    ):
        actions.append({"id": action, "tool": action, "label": label, "event_type": "confirm",
            "confirm": action != "reconcile_content_draft", "tone": "primary",
            "arguments_schema": {"type": "object", "properties": {**shared, **extra},
                "required": ["result_ref", "version", *required], "additionalProperties": False}})
    spec = _spec(DRAFT_SCENARIO, "小红书单条图文", "审核具体文案和图片后才能保存草稿。", [], actions=actions,
        form_defaults={"result_ref": data["result_ref"], "version": data["version"],
            "title": data["title"], "body": data["body"], "tags_text": "\n".join(data["tags"]),
            "image_paths_text": "\n".join(data.get("image_paths", [])), "decision": "approve", "note": ""})
    return {"type": "generation_ui", "artifact_id": artifact_id or str(uuid4()),
        "scenario": DRAFT_SCENARIO, "text": "图文候选已保存，请核实正文、来源和图片。", "spec": spec,
        "data": deepcopy(data)}
