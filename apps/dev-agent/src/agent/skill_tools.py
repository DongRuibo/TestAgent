"""TestAgent 内置 Skill 专用工具的统一注册入口。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from agent.skills import SkillCatalog
from tools.content_research import SAVE_CONTENT_RESEARCH_TOOL_NAME, ContentResearchTools
from tools.social_research import SOCIAL_RESEARCH_TOOL_NAMES, SocialResearchTools
from tools.operation_goals import GOAL_TOOL_NAMES, OperationGoalStore
from tools.promotion_plans import PROMOTION_TOOL_HANDLERS
from tools.weekly_plans import WEEKLY_TOOL_HANDLERS
from tools.research_images import IMAGE_TOOL_NAME, unavailable_images


ToolHandler = Callable[..., dict[str, Any]]


def register_skill_tools(
    handlers: Mapping[str, ToolHandler],
    catalog: SkillCatalog,
) -> dict[str, ToolHandler]:
    """为 Skill 声明的工具补齐内置处理器，不依赖具体 Skill 名称。"""
    result = dict(handlers)
    missing = set(catalog.required_tools) - set(result)
    providers: dict[str, ToolHandler] = {}
    if missing & set(GOAL_TOOL_NAMES):
        providers.update(OperationGoalStore().handlers())
    if missing & set(SOCIAL_RESEARCH_TOOL_NAMES):
        providers.update(SocialResearchTools().handlers())
    if SAVE_CONTENT_RESEARCH_TOOL_NAME in missing:
        providers.update(ContentResearchTools().handlers())
    if missing & PROMOTION_TOOL_HANDLERS.keys():
        providers.update(PROMOTION_TOOL_HANDLERS)
    if missing & WEEKLY_TOOL_HANDLERS.keys():
        providers.update(WEEKLY_TOOL_HANDLERS)
    if IMAGE_TOOL_NAME in missing:
        providers[IMAGE_TOOL_NAME] = unavailable_images

    result.update({name: providers[name] for name in missing if name in providers})
    return result


__all__ = ["register_skill_tools"]
