"""路由不执行工具；显式调用、当前任务回复和语义匹配分开处理。"""
import re

from agent.skill_recommendation import recommend_skill
from agent.skills import EXPLICIT_SKILL_PATTERN, SkillConfigurationError


def route_skill(message, catalog, *, explicit_name=None, task=None, task_reply=False):
    if explicit_name:
        return catalog.resolve(message, explicit_name=explicit_name).name, "显式指定"
    explaining = bool(re.search(r"(介绍|解释|是什么|有什么|如何使用|怎么使用)", message))
    explicit_execution = bool(re.match(r"\s*(?:【[^】\n]*】\s*)?(?:\$[a-z][a-z0-9-]*\s+|(?:请|帮我)?\s*(?:使用|调用)\s+)", message)) and not re.search(r"(是什么|有什么|如何使用|怎么使用)", message)
    if not explaining or explicit_execution:
        if EXPLICIT_SKILL_PATTERN.search(message):
            return catalog.resolve(message).name, "显式名称"
        named = re.search(r"(?:使用|调用)\s*(?:Skill\s+)?([a-z][a-z0-9]*(?:-[a-z0-9]+)+)(?:\s|$|[，。：,;])", message, re.IGNORECASE)
        if named:
            return catalog.resolve(message, explicit_name=named.group(1)).name, "显式名称"
    if task_reply:
        if not task or task.get("phase") not in {"goal_input", "goal_review", "goal_failed"}:
            raise SkillConfigurationError("当前没有等待补充的目标任务，请刷新对话。")
        if explaining or re.search(r"多少钱|收费|费用|价格|成本", message) and message.endswith(("？", "?")):
            return None, "普通说明，不修改目标或代替用户确认"
        return task["skill_name"], "继续当前任务"
    if explaining:
        return None, "普通说明，不执行 Skill"
    if (task and task.get("weekly_plan_active")
            and re.search(r"照片|素材|图文|图片|交图|选题|计划|排期|发布时间", message)
            and re.search(r"修改|调整|改一下|改为|改成|改到|来改|只用|就用|就三张|别再|不补拍", message)
            and not re.search(r"为什么|什么意思|[？?]$", message)):
        return catalog.resolve(message, explicit_name="enterprise-weekly-operation-plan").name, "调整当前周计划"
    if (task and task.get("weekly_input_pending") and task.get("phase") == "goal_ready"
            and re.search(r"照片|案例|素材|资料|提供|交图|没人|人员", message)
            and not re.search(r"[？?]|(?:不|别|不要)(?:安排|制定|生成).*计划", message)):
        return catalog.resolve(message, explicit_name="enterprise-weekly-operation-plan").name, "补充周计划资料"
    # 常见明确任务无需额外模型分类；普通问题及复杂意图交由元数据分类。
    if ("小红书" in message and any(word in message for word in ("制定", "设定", "修改", "规划", "帮我定"))
            and "目标" in message and not re.search(r"多少钱|收费|费用|价格|成本", message)):
        return catalog.resolve(message, explicit_name="operation-goal-setting").name, "目标任务规则"
    # 别名出现在费用询问、比较或引用中不等于执行意图；自动路径需语义判断。
    result = recommend_skill(message, catalog, use_rules=False)
    return result.skill_name, result.reason
