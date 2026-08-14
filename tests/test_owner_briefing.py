from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from agent.loop import (
    DEFAULT_TOOL_BUDGETS,
    DEFAULT_TOOL_HANDLERS,
    SYSTEM_PROMPT,
    TOOL_DEFINITIONS,
)
from agent.context import DEFAULT_TOOL_RESULT_MAX_CHARS, serialize_tool_result
from tools.owner_briefing import (
    OWNER_BRIEFING_TOOL_NAME,
    attach_owner_briefing_presentation,
    attach_owner_next_day_review,
    compact_owner_briefing_result,
    generate_owner_daily_briefing,
)
from tools.present_generation_ui import (
    GenerationUiPresenter,
    TrustedPresentationResultRegistry,
)
from web_agent.app import create_app
from web_agent.conversations import (
    GenerationUiActionConflictError,
    SQLiteConversationStore,
)


FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "owner_briefing_v0"


def test_owner_briefing_matches_all_seven_expected_days(tmp_path: Path) -> None:
    expected = json.loads(
        (FIXTURE_ROOT / "expected_briefings.json").read_text(encoding="utf-8")
    )

    for report_date, anomaly_keys in expected.items():
        result = generate_owner_daily_briefing(
            report_date,
            output_dir=tmp_path / "artifacts",
        )

        assert result["success"] is True
        assert result["company_name"] == "柒昱美学设计公司"
        assert {
            item["anomaly_key"] for item in result["briefing"]["anomalies"]
        } == set(anomaly_keys)
        assert all(
            item["evidence"]["source_file"]
            and item["evidence"]["record_id"]
            and item["owner"]
            and item["recommendation"]
            for item in result["briefing"]["anomalies"]
        )


def test_owner_briefing_writes_traceable_json_and_markdown(tmp_path: Path) -> None:
    result = generate_owner_daily_briefing(
        "2026-08-13",
        output_dir=tmp_path / "artifacts",
    )

    assert result["success"] is True
    briefing = result["briefing"]
    assert briefing["metrics"] == {
        "attendance_records": 4,
        "active_customers": 3,
        "today_appointments": 0,
        "project_records": 2,
        "today_starts": 1,
        "overdue_receivable_amount": 210000.0,
        "open_complaints": 1,
        "anomaly_count": 6,
        "critical_count": 1,
        "high_count": 3,
        "medium_count": 2,
    }
    assert len({item["task_id"] for item in briefing["anomalies"]}) == 6
    assistant_summary = result["assistant_summary"]
    assert "今天建议先处理 3 件事" in assistant_summary
    assert "**回款**" in assistant_summary
    assert "21 万元" in assistant_summary
    assert "**需要你确认**" in assistant_summary
    assert "其余 3 项异常" in assistant_summary
    assert not any(
        internal in assistant_summary
        for internal in ("artifacts/", ".csv", "FIN-1301", "证据：")
    )
    json_path = Path(result["artifacts"][0]["path"])
    markdown_path = Path(result["artifacts"][1]["path"])
    assert json.loads(json_path.read_text(encoding="utf-8"))["company_name"] == (
        "柒昱美学设计公司"
    )
    markdown = markdown_path.read_text(encoding="utf-8")
    assert "# 柒昱美学设计公司老板早报" in markdown
    assert "finance_cases.csv / FIN-1301" in markdown


def test_owner_briefing_rejects_invalid_or_missing_date(tmp_path: Path) -> None:
    invalid = generate_owner_daily_briefing(
        "2026/08/13",
        output_dir=tmp_path / "invalid",
    )
    missing = generate_owner_daily_briefing(
        "2026-08-14",
        output_dir=tmp_path / "missing",
    )

    assert invalid["error_type"] == "INVALID_REPORT_DATE"
    assert missing["error_type"] == "REPORT_DATE_NOT_FOUND"


def test_owner_briefing_is_registered_and_builds_trusted_ui(tmp_path: Path) -> None:
    result, artifact = _briefing_artifact(tmp_path)

    assert OWNER_BRIEFING_TOOL_NAME in TOOL_DEFINITIONS
    assert OWNER_BRIEFING_TOOL_NAME in DEFAULT_TOOL_HANDLERS
    assert DEFAULT_TOOL_BUDGETS[OWNER_BRIEFING_TOOL_NAME] == 1
    assert OWNER_BRIEFING_TOOL_NAME in SYSTEM_PROMPT
    assert result["presentation"]["scenario"] == "owner_daily_briefing"
    assert artifact["scenario"] == "owner_daily_briefing"
    assert artifact["spec"]["interaction_mode"] == "host_action"
    assert artifact["spec"]["actions"][0]["id"] == (
        "transition_owner_briefing_task"
    )
    assert artifact["data"]["metrics"]["anomaly_count"] == 6
    assert {
        item["severity_label"] for item in artifact["data"]["anomalies"]
    } == {"紧急", "重要", "一般"}
    anomaly_table = _ui_node(artifact["spec"], "anomalies")
    assert [
        column["field"] for column in anomaly_table["props"]["columns"][:3]
    ] == ["title", "severity_label", "status_label"]
    assert "anomaly_key" not in {
        column["field"] for column in anomaly_table["props"]["columns"]
    }
    anomaly_select = _ui_node(artifact["spec"], "input-anomaly_key")
    assert anomaly_select["props"]["label"] == "处理异常"
    assert anomaly_select["props"]["span"] == "full"
    assert [
        option["value"] for option in anomaly_select["props"]["options"]
    ] == [item["anomaly_key"] for item in artifact["data"]["anomalies"]]
    assert all(
        option["label"].startswith("【")
        and option["value"] not in option["label"]
        for option in anomaly_select["props"]["options"]
    )
    assert "compact_result_complete=true" in SYSTEM_PROMPT
    assert "ui_presented=true" in SYSTEM_PROMPT
    assert "可靠的个人助手 Agent" in SYSTEM_PROMPT
    assert "存在 assistant_summary" in SYSTEM_PROMPT
    assert "最多列出 3 项最高优先级异常" in SYSTEM_PROMPT


def test_owner_briefing_compact_result_keeps_all_review_items_without_truncation(
    tmp_path: Path,
) -> None:
    raw = generate_owner_daily_briefing(
        "2026-08-13", output_dir=tmp_path / "artifacts"
    )
    review = {
        "review_date": "2026-08-12",
        "generated_for": "2026-08-13",
        "metrics": {
            "new_count": 3,
            "completed_count": 0,
            "unresolved_count": 3,
            "escalated_count": 0,
            "recurring_count": 1,
        },
        "unresolved_tasks": [
            {
                "issue_key": "customer_followup_overdue:C002",
                "title": "客户乙 已 74 小时未跟进",
                "status": "detected",
                "status_label": "待处理",
                "assignee": "",
                "note": "",
                "reminder_count": 0,
                "escalation_pending": False,
                "escalation_pending_label": "否",
            },
            {
                "issue_key": "project_progress_delay:P002",
                "title": "澜庭叙项目 进度落后 17 个百分点",
                "status": "detected",
                "status_label": "待处理",
                "assignee": "",
                "note": "",
                "reminder_count": 0,
                "escalation_pending": False,
                "escalation_pending_label": "否",
            },
            {
                "issue_key": "receivable_overdue:P003",
                "title": "客户庚 应收款 50000 元逾期 2 天",
                "status": "assigned",
                "status_label": "已分派",
                "assignee": "1111",
                "note": "2222",
                "reminder_count": 0,
                "escalation_pending": False,
                "escalation_pending_label": "否",
            },
        ],
        "recurring_issues": [],
    }
    reviewed = attach_owner_next_day_review(raw, review)
    compact = compact_owner_briefing_result(
        reviewed,
        ui_result={
            "success": True,
            "artifact_id": str(uuid4()),
            "scenario": "owner_daily_briefing",
        },
    )

    serialized = serialize_tool_result(
        compact, max_chars=DEFAULT_TOOL_RESULT_MAX_CHARS
    )

    assert len(serialized) <= DEFAULT_TOOL_RESULT_MAX_CHARS
    assert json.loads(serialized).get("truncated") is not True
    assert compact["compact_result_complete"] is True
    assert compact["ui_presented"] is True
    assert compact["assistant_summary"] == raw["assistant_summary"]
    assert "presentation" not in compact
    assert [
        item["title"]
        for item in compact["briefing"]["previous_day_review"]["unresolved_tasks"]
    ] == [item["title"] for item in review["unresolved_tasks"]]
    assert compact["briefing"]["previous_day_review"]["unresolved_tasks"][2][
        "assignee"
    ] == "1111"


def test_owner_task_state_is_deduplicated_idempotent_and_audited(
    tmp_path: Path,
) -> None:
    store = SQLiteConversationStore(tmp_path / "conversations.sqlite3")
    conversation_id = store.create()
    _result, first_artifact = _briefing_artifact(tmp_path / "first")
    store.append_exchange(
        conversation_id,
        "生成早报",
        "早报已生成",
        ui_artifacts=[first_artifact],
    )
    anomaly_key = "receivable_overdue:FIN-1301"
    assign_arguments = {
        "anomaly_key": anomaly_key,
        "action": "分派",
        "assignee": "顾言",
        "note": "上午联系客户确认回款计划",
    }
    idempotency_key = str(uuid4())

    assigned = store.transition_owner_briefing_task(
        conversation_id,
        first_artifact["artifact_id"],
        arguments=assign_arguments,
        idempotency_key=idempotency_key,
    )
    repeated = store.transition_owner_briefing_task(
        conversation_id,
        first_artifact["artifact_id"],
        arguments=assign_arguments,
        idempotency_key=idempotency_key,
    )

    assert assigned == repeated
    assert assigned["status"] == "assigned"

    _result, second_artifact = _briefing_artifact(tmp_path / "second")
    store.append_exchange(
        conversation_id,
        "再次生成早报",
        "早报已重新生成",
        ui_artifacts=[second_artifact],
    )
    restored = store.generation_ui_artifact(
        conversation_id,
        second_artifact["artifact_id"],
    )
    restored_anomaly = next(
        item for item in restored.data["anomalies"] if item["anomaly_key"] == anomaly_key
    )
    assert restored_anomaly["status"] == "assigned"
    assert restored_anomaly["status_label"] == "已分派"
    assert restored_anomaly["assignee"] == "顾言"
    assert "T" not in restored_anomaly["due_at_local"]
    assert "+00:00" not in restored_anomaly["due_at_local"]

    completed = store.transition_owner_briefing_task(
        conversation_id,
        second_artifact["artifact_id"],
        arguments={
            "anomaly_key": anomaly_key,
            "action": "完成",
            "assignee": "",
            "note": "客户确认今天 16 点回款",
        },
        idempotency_key=str(uuid4()),
    )
    assert completed["status"] == "completed"
    live_options = _ui_node(
        {"root": completed["spec_patch"]["root"]},
        "input-anomaly_key",
    )["props"]["options"]
    assert anomaly_key not in {option["value"] for option in live_options}
    assert completed["form_patch"]["anomaly_key"] != anomaly_key
    assert completed["form_patch"]["action"] == "分派"
    assert completed["form_patch"]["assignee"] == ""
    assert completed["form_patch"]["note"] == ""
    refreshed = store.generation_ui_artifact(
        conversation_id,
        second_artifact["artifact_id"],
    )
    refreshed_options = _ui_node(
        refreshed.spec,
        "input-anomaly_key",
    )["props"]["options"]
    assert anomaly_key not in {option["value"] for option in refreshed_options}
    assert refreshed.spec["form_defaults"]["anomaly_key"] != anomaly_key
    with pytest.raises(GenerationUiActionConflictError, match="只有已分派任务可以升级"):
        store.transition_owner_briefing_task(
            conversation_id,
            second_artifact["artifact_id"],
            arguments={
                "anomaly_key": anomaly_key,
                "action": "升级",
                "assignee": "",
                "note": "需要老板介入",
            },
            idempotency_key=str(uuid4()),
        )

    with sqlite3.connect(store.path) as connection:
        task_count = connection.execute(
            "SELECT COUNT(*) FROM owner_briefing_tasks"
        ).fetchone()[0]
        audit_count = connection.execute(
            "SELECT COUNT(*) FROM generation_ui_actions"
        ).fetchone()[0]
    assert task_count == 6
    assert audit_count == 2


def test_owner_task_action_works_through_web_api(tmp_path: Path) -> None:
    _result, artifact = _briefing_artifact(tmp_path / "web")
    store = SQLiteConversationStore(tmp_path / "web.sqlite3")

    class FakeAgent:
        last_context_receipt = None
        last_tool_results: list[dict[str, object]] = []

        def __init__(self, trace) -> None:
            self.trace = trace
            self.generation_ui_artifacts = [artifact]

        def run(self, task, *, history=None, episode_id=None):
            self.trace("老板早报测试")
            return "老板早报已生成。"

    app = create_app(
        agent_factory=lambda trace, _scope: FakeAgent(trace),
        conversation_store=store,
    )
    client = TestClient(app)
    chat = client.post("/api/chat", json={"message": "生成 2026-08-13 老板早报"})
    assert chat.status_code == 200
    payload = chat.json()
    stored = payload["artifacts"][0]
    request = {
        "conversation_id": payload["conversation_id"],
        "message_id": int(stored["message_id"]),
        "artifact_id": stored["artifact_id"],
        "scenario": "owner_daily_briefing",
        "event_type": "confirm",
        "action_id": "transition_owner_briefing_task",
        "arguments": {
            "anomaly_key": "project_start_not_ready:PRO-1301",
            "action": "分派",
            "assignee": "顾言",
            "note": "立即确认开工负责人",
        },
        "idempotency_key": str(uuid4()),
    }

    first = client.post("/api/generation/event", json=request)
    repeated = client.post("/api/generation/event", json=request)

    assert first.status_code == 200
    assert repeated.json() == first.json()
    assert first.json()["status"] == "assigned"
    restored = client.get(f"/api/conversations/{payload['conversation_id']}").json()
    restored_anomalies = restored["messages"][1]["artifacts"][0]["data"][
        "anomalies"
    ]
    restored_task = next(
        item
        for item in restored_anomalies
        if item["anomaly_key"] == "project_start_not_ready:PRO-1301"
    )
    assert restored_task["status"] == "assigned"
    assert restored_task["assignee"] == "顾言"


def test_owner_briefing_upgrades_saved_legacy_table_columns(tmp_path: Path) -> None:
    store = SQLiteConversationStore(tmp_path / "legacy-ui.sqlite3")
    conversation_id = store.create()
    _result, artifact = _briefing_artifact(tmp_path / "legacy-ui")
    anomaly_table = next(
        card["children"][0]
        for card in artifact["spec"]["root"]["children"]
        if card["children"][0].get("props", {}).get("path") == "anomalies"
    )
    replacements = {
        "status_label": "status",
        "due_at_local": "due_at",
        "escalation_pending_label": "escalation_pending",
    }
    for column in anomaly_table["props"]["columns"]:
        column["field"] = replacements.get(column["field"], column["field"])
    artifact["spec"]["root"]["children"] = [
        card
        for card in artifact["spec"]["root"]["children"]
        if not any(
            child.get("id") == "owner-live-state-notice"
            for child in card.get("children", [])
        )
    ]
    store.append_exchange(
        conversation_id,
        "生成旧版早报",
        "旧版早报已生成",
        ui_artifacts=[artifact],
    )

    restored = store.generation_ui_artifact(
        conversation_id, artifact["artifact_id"]
    )
    restored_table = next(
        card["children"][0]
        for card in restored.spec["root"]["children"]
        if card["children"][0].get("props", {}).get("path") == "anomalies"
    )
    fields = [column["field"] for column in restored_table["props"]["columns"]]
    restored_child_ids = {
        child["id"]
        for card in restored.spec["root"]["children"]
        for child in card.get("children", [])
    }

    assert "status_label" in fields
    assert "due_at_local" in fields
    assert "escalation_pending_label" in fields
    assert "status" not in fields
    assert "owner-live-state-notice" in restored_child_ids
    assert "请以卡片状态为准" in restored.data["live_state_notice"]


def test_owner_task_creates_reminders_and_waits_for_human_escalation(
    tmp_path: Path,
) -> None:
    store = SQLiteConversationStore(tmp_path / "reminders.sqlite3")
    conversation_id = store.create()
    _result, artifact = _briefing_artifact(tmp_path / "reminders")
    store.append_exchange(
        conversation_id,
        "生成早报",
        "早报已生成",
        ui_artifacts=[artifact],
    )
    store.transition_owner_briefing_task(
        conversation_id,
        artifact["artifact_id"],
        arguments={
            "anomaly_key": "receivable_overdue:FIN-1301",
            "action": "分派",
            "assignee": "顾言",
            "note": "确认回款计划",
        },
        idempotency_key=str(uuid4()),
    )
    with sqlite3.connect(store.path) as connection:
        assigned_at = datetime.fromisoformat(
            connection.execute(
                "SELECT assigned_at FROM owner_briefing_tasks WHERE anomaly_key = ?",
                ("receivable_overdue:FIN-1301",),
            ).fetchone()[0]
        )

    reminder = store.scan_owner_briefing_reminders(
        now=assigned_at + timedelta(hours=5)
    )
    escalation = store.scan_owner_briefing_reminders(
        now=assigned_at + timedelta(hours=25)
    )

    assert reminder["reminder_count"] == 1
    assert escalation["escalation_due_count"] == 1
    events = store.owner_briefing_reminders()
    assert {item["reminder_type"] for item in events} == {
        "reminder",
        "escalation_due",
    }
    assert events[0]["status"] == "assigned"
    assert events[0]["escalation_pending"] is True


def test_owner_next_day_review_counts_results_and_recurring_issues(
    tmp_path: Path,
) -> None:
    store = SQLiteConversationStore(tmp_path / "review.sqlite3")
    conversation_id = store.create()
    _result, previous_artifact = _briefing_artifact(
        tmp_path / "previous", "2026-08-12"
    )
    store.append_exchange(
        conversation_id,
        "生成昨日早报",
        "昨日早报已生成",
        ui_artifacts=[previous_artifact],
    )
    store.transition_owner_briefing_task(
        conversation_id,
        previous_artifact["artifact_id"],
        arguments={
            "anomaly_key": "customer_followup_overdue:CUS-1202",
            "action": "分派",
            "assignee": "苏禾",
            "note": "立即跟进",
        },
        idempotency_key=str(uuid4()),
    )
    store.transition_owner_briefing_task(
        conversation_id,
        previous_artifact["artifact_id"],
        arguments={
            "anomaly_key": "customer_followup_overdue:CUS-1202",
            "action": "完成",
            "assignee": "",
            "note": "已联系但今日仍需继续跟进",
        },
        idempotency_key=str(uuid4()),
    )
    current = generate_owner_daily_briefing(
        "2026-08-13", output_dir=tmp_path / "current"
    )
    current_keys = [
        item["issue_key"] for item in current["briefing"]["anomalies"]
    ]

    review = store.owner_next_day_review(
        "2026-08-13", current_issue_keys=current_keys
    )
    reviewed = attach_owner_next_day_review(current, review)

    assert review["metrics"] == {
        "new_count": 3,
        "completed_count": 1,
        "unresolved_count": 2,
        "escalated_count": 0,
        "recurring_count": 1,
    }
    assert review["recurring_issues"][0]["issue_key"] == (
        "customer_followup_overdue:C002"
    )
    assert review["recurring_issues"][0]["previous_status_label"] == "已完成"
    assert reviewed["briefing"]["previous_day_review"] == review
    markdown = Path(reviewed["artifacts"][1]["path"]).read_text(encoding="utf-8")
    assert "## 昨日闭环复盘" in markdown
    assert "今日重复出现：1 项" in markdown


def test_owner_demo_reset_requires_explicit_endpoint_and_preserves_other_chats(
    tmp_path: Path,
) -> None:
    store = SQLiteConversationStore(tmp_path / "reset.sqlite3")
    owner_conversation_id = store.create()
    other_conversation_id = store.create()
    _result, artifact = _briefing_artifact(tmp_path / "reset")
    store.append_exchange(
        owner_conversation_id,
        "生成老板早报",
        "老板早报已生成",
        ui_artifacts=[artifact],
    )
    store.append_exchange(other_conversation_id, "普通问题", "普通回答")
    store.transition_owner_briefing_task(
        owner_conversation_id,
        artifact["artifact_id"],
        arguments={
            "anomaly_key": "receivable_overdue:FIN-1301",
            "action": "分派",
            "assignee": "顾言",
            "note": "测试",
        },
        idempotency_key=str(uuid4()),
    )
    app = create_app(conversation_store=store)
    client = TestClient(app)

    page = client.get("/")
    reset = client.post("/api/owner/demo-reset")

    assert 'id="reset-owner-demo"' in page.text
    assert reset.status_code == 200
    assert reset.json()["deleted_conversation_count"] == 1
    assert reset.json()["deleted_task_count"] == 6
    assert reset.json()["deleted_action_count"] == 1
    assert [item.conversation_id for item in store.conversation_summaries()] == [
        other_conversation_id
    ]


def _briefing_artifact(
    tmp_path: Path, report_date: str = "2026-08-13"
) -> tuple[dict, dict]:
    raw = generate_owner_daily_briefing(
        report_date,
        output_dir=tmp_path / "artifacts",
    )
    registry = TrustedPresentationResultRegistry()
    result = attach_owner_briefing_presentation(registry, raw)
    presenter = GenerationUiPresenter(registry)
    presented = presenter.present(
        "owner_daily_briefing",
        "柒昱美学设计公司老板早报",
        result["presentation"]["result_ref"],
    )
    assert presented["success"] is True
    return result, presenter.artifacts[0]


def _ui_node(spec: dict, node_id: str) -> dict:
    stack = [spec["root"]]
    while stack:
        node = stack.pop()
        if node.get("id") == node_id:
            return node
        stack.extend(node.get("children", []))
    raise AssertionError(f"UI node not found: {node_id}")
