"""单条图文的结构化候选与受控展示；审批和执行由宿主管理。"""

from copy import deepcopy
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from tools.content_operations_ui import _spec


DRAFT_SCENARIO = "content_draft"
SAVE_DRAFT_TOOL = "save_content_draft_candidate"


class DraftClaim(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    text: str = Field(min_length=1, max_length=1500,
        description="必须逐字复制本次body中实际存在的事实原句，标点也保持一致。不能填来源原句、摘要或正文没有的补充说明。")
    source_refs: list[str] = Field(min_length=1, max_length=10,
        description="只填宿主allowed_source_ids中的source_id，且必须同时列在顶层source_refs中；不得填receipt_id或拼接来源ID与回执ID。")


class DraftPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=500)
    body: str = Field(min_length=1, max_length=30000)
    tags: list[str] = Field(default_factory=list, max_length=30)
    image_notes: str = Field(min_length=1, max_length=3000,
        description="区分计划需用的图片与实际已收到、已核实授权的图片。素材安排或未来提交承诺不证明已经收到或授权；没有明确回执时写待核实，正文不能写成已提供或已授权。")
    source_refs: list[str] = Field(min_length=1, max_length=20,
        description="只填宿主allowed_source_ids中的source_id，例如S1；receipt_id仅为读取回执，不能填写或拼接成S1:receipt_id。")
    claims: list[DraftClaim] = Field(default_factory=list, max_length=30,
        description="列出正文中可外部核实的事实及其来源。每条text必须是本次body的逐字原句，不是从原始来源另抄一句。")
    missing_materials: list[str] = Field(default_factory=list, max_length=20,
        description="列出尚未收到、未核实或未确认授权的素材及事实依据。计划中要求提交、承诺未来提交或只有路径都不能当作已收到且已授权。")

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
