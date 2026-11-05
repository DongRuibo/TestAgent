"""14.2 研究工件的结构、权限和落盘测试。"""

from __future__ import annotations

from datetime import datetime, timezone
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent.loop import AgentLoop, DEFAULT_TOOL_BUDGETS, DEFAULT_TOOL_HANDLERS, TOOL_DEFINITIONS
from agent.skill_tools import register_skill_tools
from agent.skills import READ_SKILL_RESOURCE_TOOL_NAME, SkillCatalog
from testagent_dev_agent.paths import REPOSITORY_ROOT
from tools import content_research
from tools.content_operations import generate_content_weekly_plan
from tools.content_research import (
    SAVE_CONTENT_RESEARCH_TOOL_NAME,
    ContentResearchTools,
    load_content_research_artifact,
)
from tools.social_research import SOCIAL_RESEARCH_TOOL_NAMES
from tools.social_research import SocialResearchTools


def _rating(level: int) -> dict[str, object]:
    return {"level": level, "reason": "固定样例评分理由"}


def _valid_result() -> dict[str, object]:
    source = {
        "source_id": "source-1",
        "platform": "xiaohongshu",
        "post_id": "note-001",
        "url": "https://www.xiaohongshu.com/explore/note-001",
        "keyword": "上海 小户型 改造",
        "collected_at": "2026-09-04T02:00:00+00:00",
    }
    topic = {
        "title": "小户型动线改造前后对比",
        "evidence": [{"fact": "样例笔记收藏数较高", "source_refs": ["source-1"]}],
        "why_fit_company": "企业有上海小户型改造案例。",
        "target_user": "准备翻新上海小户型的业主",
        "recommended_platform": ["xiaohongshu"],
        "expected_result": ["验证用户是否愿意咨询户型诊断"],
        "required_materials": ["已获授权的改造前后图"],
        "risks": ["客户案例必须取得授权"],
        "ratings": {
            "trend": _rating(4),
            "demand": _rating(4),
            "business_fit": _rating(5),
            "commercial_value": _rating(4),
            "competition": _rating(3),
        },
        "recommendation": "high",
    }
    return {
        "company_name": "样例设计企业",
        "company_profile": "上海家装设计企业，服务小户型翻新。",
        "operation_goal": "未来一周在小红书寻找可验证咨询意向的内容方向。",
        "queries": ["上海 小户型 改造"],
        "research_summary": "固定离线样例，只验证数据结构。",
        "key_findings": [
            {
                "statement": "样例笔记收藏数为 96。",
                "kind": "fact",
                "source_refs": ["source-1"],
                "uncertainty": "离线样例，不代表实时表现。",
                "suggested_action": "真实 E2E 后再确认。",
            }
        ],
        "user_needs": [],
        "competitor_findings": [],
        "platform_findings": [],
        "source_references": [source],
        "candidate_topics": [topic, {**topic, "title": "预算拆解"}, {**topic, "title": "收纳动线"}],
        "execution_status": "partial_success",
        "limitations": ["使用离线固定样例。"],
    }


def synthetic_detail(url):
    """为宿主来源绑定提供真实经过处理器的合成正文回执。"""
    return {"success": True, "collected_at": "2026-09-04T02:00:00+00:00",
        "items": [{"url": url, "content": "合成小户型笔记正文，收藏数96，仅用于离线验证。"}]}


def test_save_content_research_result_writes_pending_artifact(tmp_path: Path) -> None:
    clock = lambda: datetime(2026, 9, 4, 2, 30, tzinfo=timezone.utc)
    result = ContentResearchTools(output_dir=tmp_path, clock=clock).save_result(
        **_valid_result()
    )

    assert result["success"] is True
    assert result["candidate_topic_count"] == 3
    assert result["approval_status"] == "pending_confirmation"
    artifact_path = Path(result["artifact_path"])
    assert artifact_path.name == "research.json"
    assert artifact_path.parent.name == result["research_id"]
    payload = artifact_path.read_text("utf-8")
    assert '"platforms": [\n    "xiaohongshu"' in payload
    assert '"research_time": "2026-09-04T02:30:00+00:00"' in payload


def test_save_content_research_result_rejects_success_without_topics(
    tmp_path: Path,
) -> None:
    payload = _valid_result()
    payload["candidate_topics"] = []

    result = ContentResearchTools(output_dir=tmp_path).save_result(**payload)

    assert result["success"] is False
    assert result["error_type"] == "INVALID_CONTENT_RESEARCH_RESULT"
    assert not list(tmp_path.iterdir())


def test_local_save_can_correct_validation_error_but_only_write_once(tmp_path):
    name = SAVE_CONTENT_RESEARCH_TOOL_NAME
    agent = AgentLoop(client=object(), trace=None, tool_definitions={name: TOOL_DEFINITIONS[name]},
        tool_handlers=ContentResearchTools(output_dir=tmp_path).handlers(), tool_budgets={name: 3})
    valid = _valid_result()
    invalid = deepcopy(valid)
    invalid["candidate_topics"][0]["why_fit_company"] = ["格式错误，不是字符串"]
    usage, cache = {}, {}
    def save(value):
        return agent._execute_tool_with_policy(name, json.dumps(value), tool_usage=usage, result_cache=cache)
    first = save(invalid)
    assert first["success"] is False
    assert first["validation_errors"][0]["field"] == "candidate_topics.0.why_fit_company"
    assert first["validation_errors"][0]["type"] == "string_type"
    assert "input" not in first["validation_errors"][0]
    assert first["retry_scope"] == "local_save_only"
    corrected = save(valid)
    assert corrected["success"] is True
    assert save(valid)["artifact_path"] == corrected["artifact_path"]
    valid["research_summary"] = "保存成功后又试图更换内容"
    assert save(valid)["error_type"] == "RESEARCH_ALREADY_SAVED"
    assert usage[name] == 2
    assert len(list(tmp_path.glob("*/research.json"))) == 1


def test_local_validation_retries_are_bounded(tmp_path):
    name = SAVE_CONTENT_RESEARCH_TOOL_NAME
    agent = AgentLoop(client=object(), trace=None, tool_definitions={name: TOOL_DEFINITIONS[name]},
        tool_handlers=ContentResearchTools(output_dir=tmp_path).handlers(), tool_budgets={name: 3})
    usage, cache = {}, {}
    value = _valid_result()
    for i in range(4):
        value["candidate_topics"][0]["why_fit_company"] = [str(i)]
        result = agent._execute_tool_with_policy(name, json.dumps(value), tool_usage=usage, result_cache=cache)
        assert result["error_type"] == ("TOOL_BUDGET_EXCEEDED" if i == 3 else "INVALID_CONTENT_RESEARCH_RESULT")
    assert usage[name] == 3
    assert not list(tmp_path.iterdir())


def test_draft_redacts_credentials_and_limits_size(monkeypatch):
    monkeypatch.setenv("GUAIKEI_API_TOKEN", "synthetic-token-for-test-only")
    cleaned = content_research.safe_research_draft({"api_key": "example-secret", "notes": [
        "synthetic-token-for-test-only", "Bearer example-credential", "password=example-pass"]})
    assert cleaned["api_key"] == "<REDACTED>"
    assert all("<REDACTED>" in value for value in cleaned["notes"])
    assert "example-credential" not in str(cleaned)
    with pytest.raises(ValueError, match="2 MB"):
        content_research.safe_research_draft({"research_summary": "x" * content_research.MAX_ARTIFACT_BYTES})


def test_load_content_research_artifact_rejects_outside_path(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "research"
    root.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("{}", "utf-8")
    monkeypatch.setattr(content_research, "CONTENT_RESEARCH_OUTPUT_DIR", root)

    with pytest.raises(ValueError, match="必须指向 14.2 工件目录"):
        load_content_research_artifact(str(outside))


def test_load_content_research_artifact_accepts_legacy_flat_path(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(content_research, "CONTENT_RESEARCH_OUTPUT_DIR", tmp_path)
    saved = ContentResearchTools(output_dir=tmp_path).save_result(**_valid_result())
    nested = Path(saved["artifact_path"])
    legacy = tmp_path / f"{saved['research_id']}.json"
    legacy.write_text(nested.read_text("utf-8"), "utf-8")

    loaded = load_content_research_artifact(str(legacy))

    assert loaded["research_id"] == saved["research_id"]


def test_repository_skill_is_valid_and_keeps_research_tools_private() -> None:
    catalog = SkillCatalog.from_directory(REPOSITORY_ROOT / ".agents" / "skills")
    skill = catalog.resolve("请做 14.2 网络数据研究与选题")

    assert skill is not None
    assert "operation-goal-setting" in catalog.names
    assert skill.name == "network-research-and-topic-selection"
    assert skill.allow_memory is False
    assert skill.allow_attachments is False
    assert skill.tool_budgets["social_search_posts"] == 6
    assert DEFAULT_TOOL_BUDGETS["social_search_posts"] == 6
    assert set(SOCIAL_RESEARCH_TOOL_NAMES) < set(skill.private_tools)
    assert SAVE_CONTENT_RESEARCH_TOOL_NAME in skill.private_tools
    assert SAVE_CONTENT_RESEARCH_TOOL_NAME in TOOL_DEFINITIONS
    assert DEFAULT_TOOL_BUDGETS[SAVE_CONTENT_RESEARCH_TOOL_NAME] == 3
    assert skill.tool_budgets[SAVE_CONTENT_RESEARCH_TOOL_NAME] == 3


def test_saved_research_artifact_enters_pending_weekly_plan(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(content_research, "CONTENT_RESEARCH_OUTPUT_DIR", tmp_path)
    saved = ContentResearchTools(output_dir=tmp_path).save_result(**_valid_result())

    plan = generate_content_weekly_plan(
        "2026-09-07",
        research_artifact_path=saved["artifact_path"],
    )

    assert plan["source_mode"] == "14.2 结构化研究工件"
    assert plan["approval_status"] == "pending_confirmation"
    assert plan["company_name"] == "样例设计企业"
    assert plan["contents"][0]["title"] == "小户型动线改造前后对比"
    assert "不能启动 RPA" in plan["rpa_boundary"]


def test_network_research_skill_offline_end_to_end(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class Provider:
        def search(self, keyword: str) -> dict[str, Any]:
            return {
                "status": "success",
                "results": [
                    {
                        "note_id": "note-001",
                        "title": "小户型动线改造",
                        "note_url": "https://www.xiaohongshu.com/explore/note-001",
                        "collected_count": 96,
                    }
                ],
            }

        def detail(self, url: str) -> dict[str, Any]:
            raise AssertionError("fixture flow does not call detail")

        def comments(self, url: str, limit: int) -> dict[str, Any]:
            raise AssertionError("fixture flow does not call comments")

        def creator_posts(self, url: str, limit: int) -> dict[str, Any]:
            raise AssertionError("fixture flow does not call creator_posts")

    class Message:
        def __init__(self, content: str | None, tool_calls: list[Any] | None = None):
            self.content = content
            self.tool_calls = tool_calls

        def model_dump(self, exclude_none: bool = True) -> dict[str, Any]:
            result: dict[str, Any] = {"role": "assistant"}
            if self.content is not None:
                result["content"] = self.content
            if self.tool_calls:
                result["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.function.name,
                            "arguments": call.function.arguments,
                        },
                    }
                    for call in self.tool_calls
                ]
            return result

    def call(call_id: str, name: str, arguments: dict[str, Any]) -> Any:
        return SimpleNamespace(
            id=call_id,
            function=SimpleNamespace(
                name=name,
                arguments=json.dumps(arguments, ensure_ascii=False),
            ),
        )

    class Client:
        def __init__(self, responses: list[Message]) -> None:
            self.responses = iter(responses)
            self.requests: list[list[dict[str, Any]]] = []
            self.tool_requests: list[list[dict[str, Any]]] = []

        def complete(self, messages, tools=None):
            self.requests.append(list(messages))
            self.tool_requests.append(list(tools or []))
            return next(self.responses)

    monkeypatch.setattr(content_research, "CONTENT_RESEARCH_OUTPUT_DIR", tmp_path)
    research_result = _valid_result()
    client = Client(
        [
            Message(
                None,
                [call("call-search", "social_search_posts", {"keyword": "上海 小户型 改造"})],
            ),
            Message(
                None,
                [call("call-save", SAVE_CONTENT_RESEARCH_TOOL_NAME, research_result)],
            ),
            Message("研究完成，候选选题等待负责人确认。"),
        ]
    )
    handlers = SocialResearchTools(
        Provider(),
        clock=lambda: datetime(2026, 9, 4, 2, tzinfo=timezone.utc),
    ).handlers()
    handlers.update(ContentResearchTools(output_dir=tmp_path).handlers())
    catalog = SkillCatalog.from_directory(REPOSITORY_ROOT / ".agents" / "skills")
    agent = AgentLoop(
        client,
        trace=None,
        tool_definitions=TOOL_DEFINITIONS,
        tool_handlers=register_skill_tools({**DEFAULT_TOOL_HANDLERS, **handlers}, catalog),
        skill_catalog=catalog,
    )

    answer = agent.run(
        "请执行 14.2 网络数据研究与选题。企业名称：样例设计企业；"
        "企业背景：上海家装设计企业，服务小户型翻新；"
        "运营目标：未来一周在小红书寻找可验证咨询意向的内容方向。"
    )

    exposed = {
        item["function"]["name"] for item in client.tool_requests[0]
    }
    assert exposed == set(SOCIAL_RESEARCH_TOOL_NAMES) | {
        "analyze_research_images",
        SAVE_CONTENT_RESEARCH_TOOL_NAME,
        READ_SKILL_RESOURCE_TOOL_NAME,
        "load_operation_goal",
    }
    assert "Skill 名称：network-research-and-topic-selection" in client.requests[0][0]["content"]
    assert "social_search_posts 最多 6 次" in client.requests[0][0]["content"]
    assert answer.endswith("等待负责人确认。")
    saved_payload = json.loads(
        next(
            item["content"]
            for item in client.requests[-1]
            if item.get("role") == "tool" and item.get("tool_call_id") == "call-save"
        )
    )
    assert saved_payload["success"] is True, saved_payload
    saved = next(
        item["result"]
        for item in agent.last_tool_results
        if item["tool_name"] == SAVE_CONTENT_RESEARCH_TOOL_NAME
    )
    plan = generate_content_weekly_plan(
        "2026-09-07", research_artifact_path=saved["artifact_path"]
    )
    assert plan["research_id"] == saved["research_id"]
