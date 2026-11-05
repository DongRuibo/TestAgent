"""Skill 激活边界与可迁移文件集；不依赖外部模型。"""

import json
from pathlib import Path
import shutil

from agent.loop import AgentLoop, DEFAULT_TOOL_HANDLERS
from agent.skill_tools import register_skill_tools
from agent.skills import SkillCatalog
from testagent_dev_agent.paths import REPOSITORY_ROOT
from tools.promotion_plans import PROMOTION_TOOL_HANDLERS
from test_agent_loop import FakeLLMClient, FakeMessage, make_tool_call


NAME = "xiaohongshu-promotion-planning"
SOURCE = REPOSITORY_ROOT / ".agents" / "skills" / NAME


def test_tools_are_scoped_to_selected_skill_and_host_mapping_is_available():
    catalog = SkillCatalog.from_directory(REPOSITORY_ROOT / ".agents" / "skills")
    handlers = register_skill_tools(DEFAULT_TOOL_HANDLERS, catalog)
    client = FakeLLMClient([FakeMessage("普通回答"), FakeMessage(None, [make_tool_call(
        "read_skill_resource", json.dumps({"path": "references/testagent.md"}))]), FakeMessage("已读取适配说明")])
    agent = AgentLoop(client, skill_catalog=catalog, tool_handlers=handlers, trace=None)
    assert agent.run("你好", auto_select_skill=False) == "普通回答"
    ordinary = {item["function"]["name"] for item in client.tool_requests[0]}
    assert not ordinary.intersection(PROMOTION_TOOL_HANDLERS)
    instructions = catalog.resolve("$" + NAME).instructions.strip()
    assert instructions not in client.requests[0][0]["content"]
    assert agent.run("请制定小红书投流计划", skill_name=NAME) == "已读取适配说明"
    active = {item["function"]["name"] for item in client.tool_requests[1]}
    assert instructions in client.requests[1][0]["content"]
    assert active == {*PROMOTION_TOOL_HANDLERS, "read_skill_resource"}
    result = json.loads(client.requests[-1][-1]["content"])
    assert result["success"] and result["path"] == "references/testagent.md"
    assert not catalog.resolve("$" + NAME).allow_memory


def test_portable_core_can_load_without_testagent_runtime_or_source_checkout(tmp_path):
    target = tmp_path / NAME
    (target / "references").mkdir(parents=True)
    for relative in ("SKILL.md", "references/plan-contract.md", "references/testagent.md", "references/promotion-experience.md"):
        shutil.copy2(SOURCE / relative, target / relative)
    assert not (target / "runtime.json").exists()
    # 文件可独立携带；runtime.json 的权限语义不假定其他宿主会理解。
    catalog = SkillCatalog.from_directory(tmp_path)
    skill = catalog.resolve("$" + NAME)
    assert skill.allowed_tools is None
    assert skill.private_tools == ()
    assert skill.read_resource("references/plan-contract.md")["success"]
    assert skill.read_resource("references/promotion-experience.md")["success"]
    body = (target / "SKILL.md").read_text("utf-8")
    assert "/Users/" not in body and "from tools" not in body
    assert "prepare_promotion_plan" not in body


def test_explicit_alias_selects_promotion_without_expanding_tools():
    catalog = SkillCatalog.from_directory(REPOSITORY_ROOT / ".agents" / "skills")
    assert catalog.resolve("帮我制定小红书投流计划").name == NAME
    client = FakeLLMClient([FakeMessage("需要明确本次输入")])
    agent = AgentLoop(client, skill_catalog=catalog,
        tool_handlers=register_skill_tools(DEFAULT_TOOL_HANDLERS, catalog), trace=None)
    assert agent.run("帮我制定小红书投流计划") == "需要明确本次输入"
    names = {item["function"]["name"] for item in client.tool_requests[0]}
    assert names == {*PROMOTION_TOOL_HANDLERS, "read_skill_resource"}
