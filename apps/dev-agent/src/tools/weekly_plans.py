"""周计划专用工具声明。处理器只能由已绑定目标的宿主流程注入。"""

from pydantic import BaseModel, ConfigDict, Field, model_validator


WEEKLY_SKILL = "enterprise-weekly-operation-plan"


class EmptyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ResearchChoice(EmptyInput):
    needs_research: bool
    reason: str = Field(min_length=1, max_length=1500,
        description="给用户的一句话业务说明，建议不超过100字，不包含工具名、字段名、ID或内部校验细节。")
    research_question: str = Field(min_length=1, max_length=2000,
        description="简洁说明问题、地区、受众、图文形式和样本数量限制，建议300字以内。保留用户的费用和调用次数约束，不重复整份目标，不写内部实现。")


class TextChange(EmptyInput):
    old: str = Field(min_length=1, max_length=12000)
    new: str = Field(max_length=12000)


class WeeklySave(EmptyInput):
    markdown: str | None = Field(default=None, min_length=100, max_length=80000)
    changes: list[TextChange] = Field(default_factory=list, max_length=20,
        description="修正保存草稿或修改原计划时，提交唯一匹配的原文old和新文new，避免重写全文。")

    @model_validator(mode="after")
    def one_form(self):
        if (self.markdown is not None) == bool(self.changes):
            raise ValueError("首次保存提交markdown；局部修正只提交changes，两者不能同时使用。")
        return self


WEEKLY_TOOL_DEFINITIONS = tuple(
    {"type": "function", "function": {"name": name, "description": description,
        "parameters": model.model_json_schema()}}
    for name, model, description in (
        ("load_weekly_plan_inputs", EmptyInput, "读取本次已绑定目标、人员说明、日期、研究候选、原计划和完整模板。只能使用此次返回的来源。"),
        ("choose_weekly_research", ResearchChoice, "规划准备阶段判断是否复用研究，记录依据和本周研究问题。不会执行研究或授权费用。记录一次后结束准备阶段。"),
        ("save_weekly_plan", WeeklySave, "编排阶段保存十项Markdown周计划。校验身份、素材、排期、来源和内容ID；失败时修正同一份草稿，不重新研究。"),
    )
)


def unbound_weekly_tool(**kwargs):
    return {"success": False, "message": "请在已确认目标的Web对话中发送“制定周计划：人员职责与本周要求”，由宿主绑定输入后运行。"}


WEEKLY_TOOL_HANDLERS = {item["function"]["name"]: unbound_weekly_tool for item in WEEKLY_TOOL_DEFINITIONS}
