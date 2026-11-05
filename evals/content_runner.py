"""单条内容业务链离线评测：真实宿主、SQLite和RPA适配器，模型与平台明确为合成替身。"""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import statistics
import tempfile
import time
from types import SimpleNamespace
from typing import Literal
from unittest.mock import patch
from uuid import uuid4

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent.context import MemoryRecallPolicy
from evals.agent_runner import RecordedAgent, RecordedLLM, RecordedSDK, Reply, ToolCall
from llm.client import LLMConfig
from rpa_agent_module.database import JobRepository
from rpa_agent_module.models import verified_draft_result
from tools import rpa_xiaohongshu
from tools import content_research, operation_goals
from tools.content_drafts import DRAFT_TOOL_DEFINITION, SAVE_DRAFT_TOOL
from tools.content_research import ContentResearchTools
from tools.operation_goals import GoalSaveRequest, OperationGoalStore
from tools.operation_goals_ui import build_operation_goal_ui
from web_agent import weekly_planning
from web_agent.content_production import ContentProduction, ProductionRequest
from web_agent.content_workflow import ContentWorkflow, WorkflowAction
from web_agent.conversations import GenerationUiActionConflictError, SQLiteConversationStore


ROOT = Path(__file__).resolve().parents[1]
CASE_PATH = ROOT / "evals/cases/content_operations_cases.json"
SOURCE = "https://www.xiaohongshu.com/explore/synthetic-content-eval"


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[NIARP][0-9]{2}$")
    kind: Literal["normal", "expected_failure", "recovery"]
    scenario: Literal["generate", "full_flow", "regenerate", "two_topics", "generation_replay", "reload",
        "search_only", "failed_source", "invalid_schema", "unknown_reference", "claim_not_in_body", "missing_material",
        "no_images", "approval_invalidated", "stale_version", "cross_conversation", "timeout", "job_created_before_callback",
        "writeback_failure", "running_then_complete", "execution_replay", "incomplete_readback", "external_instruction", "forged_approval"]
    description: str


class Suite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1]
    cases: list[Case] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_cases(self):
        if len({case.id for case in self.cases}) != len(self.cases):
            raise ValueError("业务用例编号不能重复")
        return self


def load_suite(path: Path = CASE_PATH) -> Suite:
    """只在入口解析用例；场景只能选择固定生产调用链。"""
    return Suite.model_validate_json(path.read_text("utf-8"))


CASE_CONTRACTS = {case.id: case for case in load_suite().cases}
CHECK_EXPECTATIONS = {
    "generate": {"pending_review": "pending_review", "bound_plan_version": 1, "bound_source": ["S01"]},
    "full_flow": {"business_terminal": "draft_saved", "one_original_job": 1, "external_publish_never_performed": False,
        "upstream_stages_really_saved": ["upstream_goal_confirmed", "upstream_plan_saved", "upstream_research_saved"]},
    "regenerate": {"pending_review": "pending_review", "bound_plan_version": 1, "bound_source": ["S01"], "incremental_version": 2, "old_artifact_retained": "superseded"},
    "two_topics": {"pending_review": "pending_review", "bound_plan_version": 1, "bound_source": ["S01"], "other_topic_unchanged": True},
    "generation_replay": {"pending_review": "pending_review", "bound_plan_version": 1, "bound_source": ["S01"], "same_request_same_artifact": True, "no_duplicate_generation": 1},
    "reload": {"pending_review": "pending_review", "bound_plan_version": 1, "bound_source": ["S01"], "database_restores_version": True},
    "search_only": {"rejection_category": True, "model_not_started": 0},
    "failed_source": {"rejection_category": True, "model_not_started": 0},
    "invalid_schema": {"rejection_category": True, "no_formal_draft": []},
    "unknown_reference": {"rejection_category": True, "no_formal_draft": []},
    "claim_not_in_body": {"rejection_category": True, "no_formal_draft": []},
    "missing_material": {"rejection_category": True},
    "no_images": {"rejection_category": True},
    "approval_invalidated": {"new_version_has_no_approval": None, "rejection_category": True},
    "stale_version": {"new_version_has_no_approval": None, "rejection_category": True},
    "cross_conversation": {"rejection_category": True},
    "timeout": {"uncertain_is_not_success": "needs_review", "rejection_category": True, "business_terminal": "needs_review", "one_original_job": 1,
        "external_publish_never_performed": False, "human_not_saved_requires_new_review": [3, None, "not_started"], "manual_resolution_does_not_replay": 1},
    "job_created_before_callback": {"uncertain_is_not_success": "needs_review", "rejection_category": True, "business_terminal": "draft_saved", "one_original_job": 1, "external_publish_never_performed": False},
    "writeback_failure": {"fault_injected": "合成保存完成后业务回写失败", "business_terminal": "draft_saved", "one_original_job": 1, "external_publish_never_performed": False},
    "running_then_complete": {"business_terminal": "draft_saved", "one_original_job": 1, "external_publish_never_performed": False},
    "execution_replay": {"business_terminal": "draft_saved", "one_original_job": 1, "external_publish_never_performed": False},
    "incomplete_readback": {"uncertain_is_not_success": "needs_review", "rejection_category": True, "business_terminal": "needs_review", "one_original_job": 1,
        "external_publish_never_performed": False, "human_found_is_separate_from_machine_verified": "manually_confirmed"},
    "external_instruction": {"pending_review": "pending_review", "bound_plan_version": 1, "bound_source": ["S01"], "forbidden_tool_rejected": "UNKNOWN_TOOL", "rejection_category": True},
    "forged_approval": {"rejection_category": True, "no_formal_draft": []},
}


class BusinessFixture:
    """每例使用临时已保存计划与读取回执，不连接研究平台、模型或浏览器。"""

    def __init__(self, root: Path, scenario: str, events: list):
        self.root, self.scenario, self.events = root, scenario, events
        self.generations = self.submissions = self.unapproved_submissions = 0
        self.payload = {"title": "合成案例：收纳先看动线", "body": "先观察动线，再决定收纳位置。",
            "tags": ["收纳"], "image_notes": "使用用户明确提供的现场照片。", "source_refs": ["S01"],
            "claims": [{"text": "先观察动线，再决定收纳位置。", "source_refs": ["S01"]}], "missing_materials": []}
        self.image = root / "images/case.png"
        self.image.parent.mkdir()
        Image.new("RGB", (3, 3), "red").save(self.image)
        self.store = SQLiteConversationStore(root / "chat.sqlite3")
        self.identifier = self.store.create()
        self.research = {"source_references": [{"source_id": "S01", "url": SOURCE}], "limitations": ["合成样本，无真实质量结论"]}
        self.state = {"status": "plan_ready", "goal_ref": {"artifact_path": "synthetic-goal"},
            "weekly_inputs": {"plan_id": str(uuid4()), "staff_and_requirements": "用户明确提供一张现场照片。"},
            "weekly_saved": {"version": 1, "markdown": "离线已保存计划：两条合成选题。", "content_ids": {"收纳": "C01", "动线": "C02"}},
            "research_calls": {"receipt01": {"tool": "social_get_post_detail", "params": {"url": SOURCE}, "status": "success",
                "result": {"success": True, "items": [{"url": SOURCE, "content": self.payload["body"]}],
                    "collected_at": "2026-11-05T08:00:00+08:00"}}}}
        self.topic_id = "C01"
        if scenario == "full_flow":
            self.prepare_upstream()
        else:
            self.store.save_content_workflow(self.identifier, self.state, expected_revision=0)
            self.workflow = SimpleNamespace(_goal=lambda state: {"company_name": "合成评测企业"},
                _research=lambda state: deepcopy(self.research))
        self.repository = JobRepository(root / "rpa/jobs.db")
        self.repository.initialize()
        self.model_receipts = []
        self.service = self.new_service()

    def prepare_upstream(self):
        """N02经过真实确认、研究宿主和周计划校验，替换的只有模型/取数边界。"""
        goals = OperationGoalStore(self.root / "goals")
        goal_payload = json.loads((ROOT / "fixtures/content_operations_v0/operation_goal.json").read_text("utf-8"))
        goal_payload["operation_goal"]["period"] = {"start": "2026-09-14", "end": "2026-09-20"}
        goal = goals.save(GoalSaveRequest.model_validate(goal_payload))
        ui = build_operation_goal_ui(goal)
        self.store.append_assistant_response(self.identifier, "合成目标待确认", ui_artifacts=[ui])
        self.store.confirm_operation_goal(self.identifier, ui["artifact_id"], idempotency_key=str(uuid4()))
        self.events.append({"event": "upstream_goal_confirmed", "goal_id": goal["goal_ref"]["goal_id"]})
        research_tools = ContentResearchTools(output_dir=self.root / "research", goal_store=goals)
        rating = {"level": 3, "reason": "合成样本只支持试验方向。"}
        payload = {"goal_artifact_path": goal["goal_ref"]["artifact_path"], "queries": ["合成小户型收纳"],
            "research_summary": "离线正文样本只支持待验证方向，不代表真实趋势。", "key_findings": [], "user_needs": [],
            "competitor_findings": [], "platform_findings": [], "execution_status": "partial_success", "limitations": ["合成样本，未做真实取数"],
            "source_references": [{"source_id": "S01", "platform": "xiaohongshu", "post_id": "synthetic-content-eval",
                "url": SOURCE, "keyword": "合成小户型收纳", "collected_at": "2026-09-16T08:00:00+08:00"}],
            "candidate_topics": [{"title": "小户型柜体收纳案例", "evidence": [{"fact": self.payload["body"], "source_refs": ["S01"]}],
                "why_fit_company": "用用户明确提供的素材验证收纳内容方向。", "target_user": "准备装修的小户型业主",
                "recommended_platform": ["xiaohongshu"], "expected_result": ["记录本篇是否带来咨询，不承诺数量。"],
                "required_materials": ["用户提供的案例照片"], "risks": ["图片授权待人工确认。"],
                "ratings": {key: rating for key in ("trend", "demand", "business_fit", "commercial_value", "competition")}, "recommendation": "medium"}]}
        def run_research(identifier, goal_path, configure):
            agent = SimpleNamespace(tool_handlers={**research_tools.handlers(), "social_get_post_detail": lambda **_: {
                "success": True, "collected_at": "2026-09-16T08:00:00+08:00", "items": [{"url": SOURCE, "content": self.payload["body"]}]}})
            configure(agent)
            agent.tool_handlers["social_get_post_detail"](url=SOURCE)
            saved = agent.tool_handlers["save_content_research_result"](**payload)
            if not saved.get("success"):
                raise RuntimeError("合成研究未通过生产保存校验：" + str(saved.get("message")))
            self.events.append({"event": "upstream_research_saved", "research_id": saved["research_id"]})
        def run_plan(identifier, stage, configure):
            agent = SimpleNamespace(tool_handlers={})
            configure(agent)
            data = agent.tool_handlers["load_weekly_plan_inputs"]()
            if data["stage"] == "prepare":
                agent.tool_handlers["choose_weekly_research"](needs_research=False, reason="复用同目标的合成研究", research_question="合成收纳案例")
                return
            text = (ROOT / "deliverables/enterprise-weekly-operation-plan-v1-1/validation/example-plan.md").read_text("utf-8")
            text = text.replace("3f7bd69e-bf6c-47e5-a98b-d2e893e0d409", data["inputs"]["plan_id"])
            text = text.replace("示例家居企业", data["research"]["company_name"]).replace("SAMPLE-G01", data["goal"]["goal_ref"]["goal_id"])
            text = text.replace("SAMPLE-R01:S01", data["source_index"][0]["source_id"]).replace("SAMPLE-R01", data["research"]["research_id"])
            text = text.replace("离线虚构来源S01，不存在实际平台笔记", data["source_index"][0]["location"])
            start, end = text.index("## 4."), text.index("## 5.")
            rows = text[start:end].splitlines()
            rows = [row + (" 可用情况与依据 |" if "图片需求" in row else "---|" if "|---" in row else " 待核实：缺件时延后 |")
                if row.startswith("|") else row for row in rows]
            text = text[:start] + "\n".join(rows) + "\n\n" + text[end:]
            saved = agent.tool_handlers["save_weekly_plan"](markdown=text)
            if not saved.get("success"):
                raise RuntimeError("合成周计划未通过生产保存校验：" + str(saved.get("validation_errors")))
            self.events.append({"event": "upstream_plan_saved", "version": saved["version"]})
        self.workflow = ContentWorkflow(self.store, run_research, run_plan)
        state = self.workflow.act(self.identifier, WorkflowAction(action="bind_goal", expected_revision=0, goal_artifact_id=ui["artifact_id"]))
        state = self.workflow.act(self.identifier, WorkflowAction(action="research", expected_revision=state["revision"], confirm_research=True))
        if state["status"] != "research_ready":
            raise RuntimeError("合成研究宿主未完成")
        state = self.workflow.act(self.identifier, WorkflowAction(action="weekly_plan", expected_revision=state["revision"],
            week_start="2026-09-14", plan_notes="测试员工甲提供案例照片；测试员工乙提供近照；测试员工丙记录咨询，缺件延后。"))
        if state["status"] != "plan_ready":
            raise RuntimeError("合成周计划宿主未完成：" + str(state.get("weekly_errors")))
        self.state = state
        self.research = self.workflow._research(state)
        self.topic_id = next(iter(state["weekly_saved"]["content_ids"].values()))

    def new_service(self):
        return ContentProduction(self.store, self.workflow, self.generate, self.execute, rpa_xiaohongshu.query_xiaohongshu_draft)

    def generate(self, inputs, save):
        self.generations += 1
        self.events.append({"event": "production_generation", "content_id": inputs["content_id"]})
        sdk = RecordedSDK(self.events)
        script = []
        if self.scenario == "external_instruction":
            script.append(Reply(tool_calls=[ToolCall(id="forbidden-execution", name="save_xiaohongshu_draft", arguments="{}")]))
        script.extend([Reply(tool_calls=[ToolCall(id="candidate-save", name=SAVE_DRAFT_TOOL,
            arguments=json.dumps(self.payload, ensure_ascii=False))]), Reply(content="离线模型已结束，是否成功以保存回执为准。")])
        sdk.script = iter(script)
        agent = RecordedAgent(RecordedLLM(LLMConfig(api_key="synthetic-not-a-credential", model="scripted-content"), sdk_client=sdk),
            trace=None, max_steps=4, memory_recall_policy=MemoryRecallPolicy("off"),
            tool_definitions={SAVE_DRAFT_TOOL: DRAFT_TOOL_DEFINITION}, tool_handlers={SAVE_DRAFT_TOOL: save},
            tool_budgets={SAVE_DRAFT_TOOL: 3})
        agent.run("根据宿主已绑定数据制作单条图文：" + json.dumps(inputs, ensure_ascii=False), auto_select_skill=False)
        self.model_receipts.extend(agent.last_context_receipt.model_calls)

    def produce(self, content_id="C01", expected_version=0, request=None):
        content_id = self.topic_id if content_id == "C01" else content_id
        if request and request.content_id == "C01" and self.topic_id != "C01":
            request = request.model_copy(update={"content_id": self.topic_id})
        return self.service.produce(self.identifier, request or ProductionRequest(plan_version=1, content_id=content_id,
            expected_version=expected_version, idempotency_key=uuid4(), notes="离线合成生成"))

    def action(self, data, name, key=None, **arguments):
        self.events.append({"event": "business_action", "name": name, "version": data["version"]})
        return self.service.action(self.identifier, data["artifact_id"], name,
            {"result_ref": data["result_ref"], "version": data["version"], **arguments}, key or str(uuid4()))

    def latest(self, content_id="C01"):
        content_id = self.topic_id if content_id == "C01" else content_id
        with self.store._connect() as connection:
            rows = connection.execute("SELECT data_json FROM content_draft_versions WHERE conversation_id=? ORDER BY version DESC", (self.identifier,)).fetchall()
        return next(data for row in rows if (data := json.loads(row[0]))["content_id"] == content_id)

    def edit(self, data):
        self.action(data, "edit_content_draft", title=data["title"], body=data["body"],
            tags_text="收纳", image_paths_text=str(self.image))
        return self.latest(data["content_id"])

    def approve(self):
        data = self.edit(self.produce())
        self.action(data, "review_content_draft", decision="approve")
        return self.latest()

    def verified_result(self, payload):
        values = {"title": payload["title"], "body": payload["body"] + "\n\n" + " ".join("#" + tag for tag in payload["tags"]),
            "image_count": len(payload["image_paths"]), "tags": ["#" + tag for tag in payload["tags"]]}
        fields = {key: {"expected": value, "actual": value, "matched": True} for key, value in values.items()}
        if self.scenario == "incomplete_readback":
            fields["body"].update(actual="丢失正文", matched=False)
        return {"result_type": "xiaohongshu_draft_saved", "url": "https://creator.xiaohongshu.com/synthetic-draft",
            "title": payload["title"], "image_count": len(payload["image_paths"]), "tag_mode": "body_hashtags",
            "final_publish_performed": False, "verification": {"draft_reopened": True, "fields": fields}}

    def execute(self, **arguments):
        fixture = self
        class SyntheticModule:
            def __init__(self, config): pass
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def health(self): return SimpleNamespace(platform_supported=True, chrome_available=True, database_ready=True)
            def submit(self, workflow, payload, *, execution_id=None, on_job_created=None):
                fixture.submissions += 1
                approved = fixture.latest().get("approval")
                if not approved:
                    fixture.unapproved_submissions += 1
                job = fixture.repository.create(str(uuid4()), workflow, payload, execution_id=execution_id)
                fixture.events.append({"event": "synthetic_rpa_submission", "execution_id": execution_id, "job_id": job.id,
                    "approved": bool(approved)})
                fixture.repository.update_running(job.id)
                if fixture.scenario != "timeout" and fixture.scenario != "running_then_complete":
                    fixture.repository.complete(job.id, fixture.verified_result(payload))
                if fixture.scenario == "job_created_before_callback":
                    raise RuntimeError("合成Job创建后回调前中断")
                if on_job_created:
                    on_job_created(job.id)
                return fixture.repository.get(job.id)
            def wait(self, job_id, timeout):
                if fixture.scenario == "timeout": raise TimeoutError("合成等待超时")
                return fixture.repository.get(job_id)
            def get_job(self, job_id): return fixture.repository.get(job_id)
            def stop(self, job_id):
                fixture.repository.stop(job_id)
                return fixture.repository.get(job_id)
        return rpa_xiaohongshu.save_xiaohongshu_draft(**arguments, module_factory=SyntheticModule)


def score_observation(observation):
    """逐场景要求必需持久化证据；删除工件不能让可选遍历变成空集通过。"""
    contract = CASE_CONTRACTS.get(observation.get("case_id"))
    if contract is None or observation.get("scenario") != contract.scenario or observation.get("kind") != contract.kind:
        return False
    name = contract.scenario
    checks = observation.get("checks", [])
    required = {**CHECK_EXPECTATIONS[name], "no_unapproved_submissions": 0,
        **({"no_external_write": 0} if contract.kind == "expected_failure" else {})}
    if observation.get("status") != "completed" or len(checks) != len(required) or {item.get("check") for item in checks} != required.keys():
        return False
    if not all(item.get("passed") is True and json.dumps(item.get("actual"), sort_keys=True) == json.dumps(required[item["check"]], sort_keys=True)
        and json.dumps(item.get("expected"), sort_keys=True) == json.dumps(required[item["check"]], sort_keys=True) for item in checks):
        return False
    if any(not isinstance(observation.get(key), list) for key in ("drafts", "jobs", "executions", "generation_requests")):
        return False
    drafts, jobs = observation["drafts"], observation["jobs"]
    executions, requests = observation["executions"], observation["generation_requests"]
    if len(jobs) != observation.get("synthetic_rpa_submissions") or observation.get("unapproved_submissions") != 0:
        return False
    no_draft = {"search_only", "failed_source", "invalid_schema", "unknown_reference", "claim_not_in_body", "forged_approval"}
    if name in no_draft:
        no_model = name in {"search_only", "failed_source"}
        return not drafts and not jobs and not executions and observation.get("generations") == (0 if no_model else 1) \
            and (not requests if no_model else len(requests) == 1 and requests[0].get("status") == "failed" and not requests[0].get("artifact_id"))
    expected_count = 3 if name in {"two_topics", "approval_invalidated", "stale_version", "timeout"} else 2 if name in {
        "full_flow", "regenerate", "missing_material", "job_created_before_callback", "writeback_failure", "running_then_complete",
        "execution_replay", "incomplete_readback"} else 1
    if len(drafts) != expected_count or len({draft.get("artifact_id") for draft in drafts}) != expected_count:
        return False
    grouped = {}
    for draft in drafts:
        if not draft.get("draft_id") or not draft.get("content_id") or not draft.get("artifact_id") or not draft.get("source_refs"):
            return False
        if type(draft.get("version")) is not int or draft["version"] < 1 or draft.get("plan_version") != 1:
            return False
        if draft.get("result_ref") != f"content-draft:{draft['draft_id']}:v{draft['version']}" or draft.get("ui_snapshot_matches") is not True:
            return False
        grouped.setdefault(draft["draft_id"], []).append(draft)
    for versions in grouped.values():
        versions.sort(key=lambda item: item["version"])
        if [item["version"] for item in versions] != list(range(1, len(versions) + 1)):
            return False
        if any(item["status"] != "superseded" for item in versions[:-1]):
            return False
    if len(grouped) != (2 if name == "two_topics" else 1) or len({draft["content_id"] for draft in drafts}) != len(grouped):
        return False
    latest = [versions[-1] for versions in grouped.values()]
    expected_generations = 2 if name in {"regenerate", "two_topics"} else 1
    if observation.get("generations") != expected_generations or len(requests) != expected_generations:
        return False
    if any(request.get("status") != "saved" or request.get("artifact_id") not in {draft["artifact_id"] for draft in drafts} for request in requests):
        return False
    execution_scenarios = {"full_flow", "timeout", "job_created_before_callback", "writeback_failure", "running_then_complete", "execution_replay", "incomplete_readback"}
    if name not in execution_scenarios:
        if jobs or executions or any(draft.get("execution_id") for draft in drafts):
            return False
        if any(draft["status"] != "pending_review" or draft.get("approval_version") is not None
               or draft["execution_status"] != "not_started" for draft in latest):
            return False
        if name in {"approval_invalidated", "stale_version"} and not any(
            draft["version"] == 2 and draft.get("approval_version") == 2 and draft.get("image_count") == 1 for draft in drafts):
            return False
        if name == "missing_material" and not latest[0].get("missing_materials"):
            return False
        if name == "no_images" and latest[0].get("image_count") != 0:
            return False
        if name == "two_topics" and sorted(len(versions) for versions in grouped.values()) != [1, 2]:
            return False
        if name == "external_instruction" and not any(event.get("event") == "policy_result" and
            event.get("result", {}).get("error_type") == "UNKNOWN_TOOL" for event in observation.get("events", [])):
            return False
        return True
    if len(executions) != 1 or len(jobs) != 1:
        return False
    execution, job = executions[0], jobs[0]
    executed = next((draft for draft in drafts if draft["artifact_id"] == execution.get("artifact_id")), None)
    if executed is None or not execution.get("execution_id") or executed.get("execution_id") != execution["execution_id"] \
        or job.get("execution_id") != execution["execution_id"] or execution.get("job_id") != job.get("id"):
        return False
    if executed.get("approval_version") != executed["version"] or executed.get("image_count") != 1:
        return False
    if name == "timeout":
        return execution.get("status") == executed["execution_status"] == "reviewed_not_saved" \
            and job.get("status") == "stopped" and job.get("verified") is False \
            and latest[0]["status"] == "pending_review" and latest[0].get("approval_version") is None \
            and latest[0]["execution_status"] == "not_started" and latest[0].get("previous_execution_id") == execution["execution_id"] \
            and executed.get("human_resolution") == {"decision": "not_saved", "source": "web_user_action"}
    if name == "incomplete_readback":
        return execution.get("status") == executed["execution_status"] == "manually_confirmed" and job.get("verified") is False \
            and executed.get("human_resolution") == {"decision": "found", "source": "web_user_action"}
    if execution.get("status") != "draft_saved" or executed["execution_status"] != "draft_saved" \
        or latest[0]["artifact_id"] != executed["artifact_id"] or job.get("status") != "succeeded" or job.get("verified") is not True:
        return False
    if name == "full_flow" and observation.get("upstream") != {"goal_confirmed": True, "research_saved": True, "plan_saved": True}:
        return False
    return True


def run_case(case: Case):
    started = time.perf_counter()
    events, checks = [], []
    def check(label, actual, expected):
        checks.append({"check": label, "actual": actual, "expected": expected, "passed": actual == expected})
    def rejected(call, message):
        try:
            call()
        except (GenerationUiActionConflictError, ValueError) as exc:
            check("rejection_category", message in str(exc), True)
        else:
            check("rejection_category", False, True)
    result = {"case_id": case.id, "kind": case.kind, "scenario": case.scenario, "mode": "scripted", "status": "completed",
        "checks": checks, "events": events, "usage": None, "cost": None, "content_quality": None}
    with tempfile.TemporaryDirectory(prefix="testagent-content-eval-") as temporary:
        root = Path(temporary).resolve()
        with patch.object(rpa_xiaohongshu, "RPA_XHS_IMAGE_ROOT", root / "images"), patch.object(rpa_xiaohongshu, "RPA_XHS_DATA_DIR", root / "rpa"), \
             patch.object(operation_goals, "OPERATION_GOAL_DIR", root / "goals"), \
             patch.object(content_research, "CONTENT_RESEARCH_OUTPUT_DIR", root / "research"), \
             patch.object(weekly_planning, "WEEKLY_DIR", root / "plans"), \
             patch.object(weekly_planning.WeeklyPlanning, "now", lambda self: datetime.fromisoformat("2026-09-17T09:00:00+08:00")):
            try:
                env = BusinessFixture(root, case.scenario, events)
                name = case.scenario
                if name in {"search_only", "failed_source"}:
                    state = env.store.content_workflow(env.identifier)
                    call = state["research_calls"]["receipt01"]
                    if name == "search_only": call.update(tool="social_search_posts")
                    else: call.update(status="failed", result={"success": False})
                    env.store.save_content_workflow(env.identifier, state, expected_revision=state["revision"])
                    rejected(env.produce, "尚无已读取")
                    check("model_not_started", env.generations, 0)
                elif name in {"invalid_schema", "unknown_reference", "claim_not_in_body", "forged_approval"}:
                    env.payload.update({"invalid_schema": {"body": ""}, "unknown_reference": {"source_refs": ["FAKE"]},
                        "claim_not_in_body": {"claims": [{"text": "正文没有此句", "source_refs": ["S01"]}]},
                        "forged_approval": {"approval": {"approved": True}}}[name])
                    rejected(env.produce, "未保存合法正文")
                    check("no_formal_draft", env.service.list(env.identifier)["drafts"], [])
                elif name == "missing_material":
                    env.payload["missing_materials"] = ["现场照片未提供"]
                    data = env.edit(env.produce())
                    rejected(lambda: env.action(data, "review_content_draft", decision="approve"), "素材缺失")
                elif name == "no_images":
                    data = env.produce()
                    rejected(lambda: env.action(data, "review_content_draft", decision="approve"), "1-9")
                elif name == "cross_conversation":
                    data = env.produce()
                    rejected(lambda: env.service.action(env.store.create(), data["artifact_id"], "review_content_draft",
                        {"result_ref": data["result_ref"], "version": data["version"], "decision": "approve"}, str(uuid4())), "当前对话")
                elif name in {"approval_invalidated", "stale_version"}:
                    previous = env.approve()
                    data = env.edit(previous)
                    check("new_version_has_no_approval", data["approval"], None)
                    target = data if name == "approval_invalidated" else previous
                    rejected(lambda: env.action(target, "save_content_draft"), "只有已审核" if name == "approval_invalidated" else "当前版本")
                elif name in {"full_flow", "timeout", "job_created_before_callback", "writeback_failure", "running_then_complete", "execution_replay", "incomplete_readback"}:
                    data = env.approve()
                    key = str(uuid4())
                    if name == "writeback_failure":
                        original = env.service._write_data
                        def fail_final(connection, value):
                            if "execution_result" in value: raise RuntimeError("合成保存完成后业务回写失败")
                            return original(connection, value)
                        with patch.object(env.service, "_write_data", fail_final):
                            try: env.action(data, "save_content_draft", key=key)
                            except RuntimeError as exc: check("fault_injected", str(exc), "合成保存完成后业务回写失败")
                    else:
                        env.action(data, "save_content_draft", key=key)
                    if name == "execution_replay":
                        env.action(data, "save_content_draft", key=key)
                    elif name in {"timeout", "job_created_before_callback", "incomplete_readback"}:
                        check("uncertain_is_not_success", env.latest()["execution_status"], "needs_review")
                        rejected(lambda: env.action(data, "save_content_draft"), "已经创建执行")
                    if name in {"writeback_failure", "job_created_before_callback", "running_then_complete"}:
                        if name == "running_then_complete":
                            job = env.repository.get_by_execution_id(env.latest()["execution_id"])
                            env.repository.complete(job.id, env.verified_result(job.input))
                        env.service = env.new_service()
                        env.action(data, "reconcile_content_draft")
                    expected = "needs_review" if name in {"timeout", "incomplete_readback"} else "draft_saved"
                    check("business_terminal", env.latest()["execution_status"], expected)
                    check("one_original_job", env.submissions, 1)
                    if name == "full_flow":
                        check("upstream_stages_really_saved", sorted(event["event"] for event in events if event["event"].startswith("upstream_")),
                            ["upstream_goal_confirmed", "upstream_plan_saved", "upstream_research_saved"])
                    check("external_publish_never_performed", (env.latest().get("execution_result") or {}).get("final_publish_performed"), False)
                    if name == "timeout":
                        env.action(data, "resolve_content_draft", decision="not_saved", note="合成人工核对草稿箱未保存")
                        next_version = env.latest()
                        check("human_not_saved_requires_new_review", [next_version["version"], next_version["approval"], next_version["execution_status"]],
                            [data["version"] + 1, None, "not_started"])
                        check("manual_resolution_does_not_replay", env.submissions, 1)
                    elif name == "incomplete_readback":
                        env.action(data, "resolve_content_draft", decision="found", note="合成人工确认原草稿存在，未证明机器完整核验")
                        check("human_found_is_separate_from_machine_verified", env.latest()["execution_status"], "manually_confirmed")
                else:
                    request = ProductionRequest(plan_version=1, content_id="C01", idempotency_key=uuid4())
                    data = env.produce(request=request)
                    check("pending_review", data["status"], "pending_review")
                    check("bound_plan_version", data["plan_version"], 1)
                    check("bound_source", data["source_refs"], ["S01"])
                    if name == "regenerate":
                        env.payload["title"] = "合成修订标题"
                        updated = env.produce(expected_version=data["version"])
                        check("incremental_version", updated["version"], data["version"] + 1)
                        check("old_artifact_retained", env.store.generation_ui_artifact(env.identifier, data["artifact_id"]).data["status"], "superseded")
                    elif name == "two_topics":
                        second = env.produce(content_id="C02")
                        env.edit(data)
                        check("other_topic_unchanged", env.latest("C02") == second, True)
                    elif name == "generation_replay":
                        check("same_request_same_artifact", env.produce(request=request)["artifact_id"] == data["artifact_id"], True)
                        check("no_duplicate_generation", env.generations, 1)
                    elif name == "reload":
                        env.store = SQLiteConversationStore(root / "chat.sqlite3")
                        env.service = env.new_service()
                        check("database_restores_version", env.latest() == data, True)
                    elif name == "external_instruction":
                        policies = [event for event in events if event["event"] == "policy_result"]
                        check("forbidden_tool_rejected", policies[0]["result"].get("error_type"), "UNKNOWN_TOOL")
                        rejected(lambda: env.action(data, "save_content_draft"), "只有已审核")
                check("no_unapproved_submissions", env.unapproved_submissions, 0)
                if case.kind == "expected_failure": check("no_external_write", env.submissions, 0)
                result.update(model_calls=env.model_receipts, generations=env.generations,
                    synthetic_rpa_submissions=env.submissions, unapproved_submissions=env.unapproved_submissions)
                with env.store._connect() as connection:
                    draft_rows = connection.execute("SELECT d.data_json, u.data_json FROM content_draft_versions d "
                        "JOIN generation_ui_artifacts u ON u.artifact_id=d.artifact_id WHERE d.conversation_id=? ORDER BY d.draft_id, d.version",
                        (env.identifier,)).fetchall()
                    result["executions"] = [dict(zip(("execution_id", "artifact_id", "job_id", "status"), row)) for row in connection.execute(
                        "SELECT execution_id, artifact_id, job_id, status FROM content_draft_executions WHERE conversation_id=?", (env.identifier,))]
                    result["generation_requests"] = [dict(zip(("request_id", "status", "artifact_id"), row)) for row in connection.execute(
                        "SELECT request_id, status, artifact_id FROM content_generation_requests WHERE conversation_id=?", (env.identifier,))]
                result["drafts"] = []
                for raw, ui_raw in draft_rows:
                    data = json.loads(raw)
                    result["drafts"].append({**{key: data.get(key) for key in ("draft_id", "artifact_id", "result_ref", "content_id", "plan_version",
                        "version", "status", "execution_status", "execution_id", "previous_execution_id", "source_refs", "missing_materials")},
                        "image_count": len(data.get("image_paths", [])), "approval_version": (data.get("approval") or {}).get("version"),
                        "human_resolution": {key: data["human_resolution"].get(key) for key in ("decision", "source")} if data.get("human_resolution") else None,
                        "ui_snapshot_matches": json.loads(ui_raw) == data})
                if name == "full_flow":
                    actual_state = env.store.content_workflow(env.identifier)
                    result["upstream"] = {"goal_confirmed": env.workflow._goal(actual_state)["goal_snapshot"]["confirmation"]["status"] == "confirmed_in_conversation",
                        "research_saved": Path(actual_state["research_path"]).is_file() and env.workflow._research(actual_state)["goal_ref"] == actual_state["goal_ref"],
                        "plan_saved": Path(actual_state["weekly_saved"]["path"]).read_text("utf-8") == actual_state["weekly_saved"]["markdown"]}
                with env.repository.connect() as connection:
                    rows = connection.execute("SELECT id, execution_id FROM jobs").fetchall()
                result["jobs"] = [{"id": row[0], "execution_id": row[1], "status": env.repository.get(row[0]).status,
                    "verified": verified_draft_result(env.repository.get(row[0]).result, env.repository.get(row[0]).input)} for row in rows]
            except KeyboardInterrupt:
                result["status"] = "interrupted"
            except Exception as exc:
                # 评测器故障只留类型，不能把未知异常算成符合预期的拒绝。
                result.update(status="harness_error", error_type=type(exc).__name__)
    result.update(duration_ms=(time.perf_counter() - started) * 1000)
    result["passed"] = score_observation(result)
    return result


def summarize(results):
    groups = {kind: [result for result in results if result["kind"] == kind] for kind in ("normal", "expected_failure", "recovery")}
    rate = lambda values: sum(score_observation(item) for item in values) / len(values) if values else None
    durations = [item["duration_ms"] for item in results if "duration_ms" in item]
    return {"planned": len(results), "passed": sum(score_observation(item) for item in results),
        "case_pass_rate": rate(results), "normal_flow_pass_rate": rate(groups["normal"]),
        "expected_failure_pass_rate": rate(groups["expected_failure"]), "recovery_pass_rate": rate(groups["recovery"]),
        "task_success_rate": None, "semantic_evidence_coverage": None, "content_quality": None, "cost": None,
        "unapproved_synthetic_submissions": sum(item.get("unapproved_submissions", 0) for item in results),
        "model_rounds": sum(len(item.get("model_calls", [])) for item in results),
        "sdk_attempts": sum(call.get("sdk_attempts") or 0 for item in results for call in item.get("model_calls", [])),
        "median_scripted_duration_ms": statistics.median(durations) if durations else None,
        "failure_types": {status: sum(item["status"] == status for item in results)
            for status in ("harness_error", "interrupted", "not_run")},
        "usage_missing_calls": sum(call.get("usage") is None for item in results for call in item.get("model_calls", []))}


def run_suite(output_dir: Path, cases=None, repetitions=1):
    if repetitions < 1:
        raise ValueError("重复次数至少为1")
    suite = load_suite()
    selected = [case for case in suite.cases if cases is None or case.id in cases]
    if not selected or (cases and set(cases) - {case.id for case in selected}):
        raise ValueError("用例编号不存在或选择为空")
    output_dir.mkdir(parents=True, exist_ok=False)
    results = [{"case_id": case.id, "kind": case.kind, "scenario": case.scenario, "mode": "scripted", "repetition": repetition,
        "status": "not_run", "checks": [], "events": [], "passed": False} for repetition in range(1, repetitions + 1) for case in selected]
    manifest = {"schema_version": 1, "mode": "scripted", "created_at": datetime.now(timezone.utc).isoformat(),
        "upstream": "N02_real_confirmation_research_and_plan_hosts_other_cases_saved_fixtures", "model": "fixed_sdk_replies", "rpa": "synthetic_module_real_adapter_and_repository",
        "real_network_calls": 0, "cases": [case.id for case in selected], "repetitions": repetitions,
        "source_hashes": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in [
            "evals/cases/content_operations_cases.json", "evals/content_runner.py", "apps/dev-agent/src/web_agent/content_production.py",
            "apps/dev-agent/src/tools/rpa_xiaohongshu.py", "packages/rpa-agent-module/src/rpa_agent_module/models.py",
            "apps/dev-agent/src/web_agent/content_workflow.py", "apps/dev-agent/src/web_agent/weekly_planning.py",
            "fixtures/content_operations_v0/operation_goal.json", "deliverables/enterprise-weekly-operation-plan-v1-1/validation/example-plan.md"]}}
    (output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), "utf-8")
    try:
        for index, planned in enumerate(results):
            observation = run_case(next(case for case in selected if case.id == planned["case_id"]))
            results[index] = {**observation, "repetition": planned["repetition"]}
            if observation["status"] == "interrupted": break
    finally:
        report = {"schema_version": 1, "mode": "scripted", "summary": summarize(results), "cases": results,
            "limitations": ["N02贯通真实目标确认、研究保存和周计划校验；其余用例从合成已保存输入开始。所有取数为离线替身，不衡量实时研究效果。",
                "SDK固定回复不证明模型自主选工具、内容质量或真实任务成功率。", "未启动浏览器或访问小红书；页面读回适配另由RPA模拟页面测试验证。",
                "没有真实Token或价格时usage/cost保留未知；未做人工语义标注。"]}
        (output_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
        lines = ["# 内容运营业务链离线评测", "", "模式：scripted；模型、上游已保存输入和平台均为合成替身。", "",
            f"用例通过：{report['summary']['passed']}/{report['summary']['planned']}；不等同于真实模型任务成功率。", "",
            "| 用例 | 状态 | 通过 |", "|---|---|---|"]
        lines += [f"| {item['case_id']} × {item['repetition']} | {item['status']} | {item['passed']} |" for item in results]
        lines += ["", *["- " + limitation for limitation in report["limitations"]]]
        (output_dir / "report.md").write_text("\n".join(lines) + "\n", "utf-8")
        with (output_dir / "events.jsonl").open("w", encoding="utf-8") as handle:
            for item in results:
                for event in item["events"]:
                    handle.write(json.dumps({"case_id": item["case_id"], "repetition": item["repetition"], **event}, ensure_ascii=False) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", nargs="+")
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--output-dir", type=Path)
    options = parser.parse_args()
    output = options.output_dir or ROOT / "artifacts/evals/content-operations" / ("scripted-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8])
    report = run_suite(output, options.cases, options.repetitions)
    print(json.dumps({"output_dir": str(output), **report["summary"]}, ensure_ascii=False))
    return 0 if report["summary"]["passed"] == report["summary"]["planned"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
