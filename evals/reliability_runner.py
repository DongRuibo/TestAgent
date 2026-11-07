"""独立离线可靠性基线：复用已有评测，增加真实宿主和 JobManager 的故障探针。"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import platform
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing import Literal

from agent.context import ContextConfig, MemoryRecallPolicy
from evals import agent_runner, content_runner
from llm.client import LLMConfig
from rpa_agent_module.browser_session import ManagedBrowserSession, XiaohongshuBrowserSessionManager
from rpa_agent_module.database import JobRepository
from rpa_agent_module.jobs import JobManager
from rpa_agent_module.models import XiaohongshuDraftRequest, verified_draft_result
from rpa_agent_module.module import RpaExecutionModule
from tools import content_research, operation_goals, rpa_xiaohongshu
from tools.guaikei import GuaikeiError
from tools.social_research import SOCIAL_GET_POST_DETAIL_TOOL, SocialResearchTools
from web_agent import weekly_planning
from web_agent.content_production import ContentProduction
from web_agent.conversations import GenerationUiActionConflictError, SQLiteConversationStore


ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "evals/datasets/reliability_v1.json"


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: str
    suite: Literal["agent", "content", "reliability"]
    source_case: str
    user_goal: str
    initial_state: dict
    available_tools: list[str]
    fault_condition: str | None
    expected_tool_behavior: dict
    expected_final_state: dict
    forbidden_side_effects: list[str]
    tags: list[str]


class Dataset(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal["1.0.0"]
    cases: list[Case] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_ids(self):
        if len({case.case_id for case in self.cases}) != len(self.cases):
            raise ValueError("用例编号重复")
        return self


def load_dataset(path=DATASET):
    """外部用例只在入口校验；运行过程中使用已定型的值。"""
    return Dataset.model_validate_json(Path(path).read_text("utf-8"))


@contextmanager
def isolated(root):
    """所有可写业务入口绑定合成目录，禁止 Python socket 对外连接。"""
    with ExitStack() as stack:
        for name in ("connect", "connect_ex"):
            stack.enter_context(patch.object(socket.socket, name, side_effect=AssertionError("离线评测禁止网络连接")))
        stack.enter_context(patch.object(ContextConfig, "from_env", return_value=ContextConfig()))
        stack.enter_context(patch.object(agent_runner.AgentLoop, "_current_datetime",
            lambda self: agent_runner.REFERENCE_TIME.astimezone(self.timezone)))
        for module, name, value in (
            (rpa_xiaohongshu, "RPA_XHS_IMAGE_ROOT", root / "images"),
            (rpa_xiaohongshu, "RPA_XHS_DATA_DIR", root / "rpa"),
            (operation_goals, "OPERATION_GOAL_DIR", root / "goals"),
            (content_research, "CONTENT_RESEARCH_OUTPUT_DIR", root / "research"),
            (weekly_planning, "WEEKLY_DIR", root / "plans"),
        ):
            stack.enter_context(patch.object(module, name, value))
        stack.enter_context(patch.object(weekly_planning.WeeklyPlanning, "now",
            lambda self: datetime.fromisoformat("2026-09-17T09:00:00+08:00")))
        yield


def read_effects(root):
    path = root / "effects.jsonl"
    return [json.loads(line) for line in path.read_text("utf-8").splitlines()] if path.exists() else []


@contextmanager
def simulated_robot(root, events, *, incomplete=False):
    """只替换 Chrome 和 Robot 出口，保留真实模块锁、提交、仓库及适配器。"""
    def robot(manager, job_id, workflow, input_file, result_file, job_dir):
        job = manager.repository.get(job_id)
        manager.repository.update_running(job_id)
        payload = job.input
        body = payload["body"] + "\n\n" + " ".join("#" + tag for tag in payload["tags"])
        values = {"title": payload["title"], "body": body, "image_count": len(payload["image_paths"]),
            "tags": ["#" + tag for tag in payload["tags"]]}
        fields = {name: {"expected": value, "actual": value, "matched": True} for name, value in values.items()}
        result = {"result_type": "xiaohongshu_draft_saved", "title": payload["title"],
            "url": "https://creator.xiaohongshu.com/synthetic-draft", "image_count": len(payload["image_paths"]),
            "tag_mode": "body_hashtags", "final_publish_performed": False,
            "verification": {"draft_reopened": True, "fields": fields}}
        # 独立副作用账本不从业务成功状态推断，先记录平台提交，再保存读回回执。
        effect = {"operation": "save_draft", "execution_id": job.execution_id, "job_id": job_id,
            "title": payload["title"], "body": body, "image_count": len(payload["image_paths"]), "published": False}
        with (root / "effects.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(effect, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        events.append({"event": "external_effect", **effect})
        if incomplete:
            fields["body"].update(actual="不完整正文", matched=False)
        Path(result_file).write_text(json.dumps(result, ensure_ascii=False), "utf-8")
        manager._finalize_robot_result(job_id, 0, Path(result_file), workflow)

    browser = ManagedBrowserSession(1, 9222, root / "synthetic-profile", "synthetic-tab")
    with patch.object(JobManager, "_start_thread", robot), \
         patch.object(XiaohongshuBrowserSessionManager, "ensure", return_value=browser), \
         patch.object(RpaExecutionModule, "health", return_value=SimpleNamespace(
             platform_supported=True, chrome_available=True, database_ready=True)), \
         patch.object(RpaExecutionModule, "_chrome_available", return_value=True):
        yield


def snapshot(env):
    """评分使用 SQLite 读回和独立副作用记录，保留可复核的原始证据。"""
    with env.store._connect() as db:
        drafts = [json.loads(row[0]) for row in db.execute("SELECT data_json FROM content_draft_versions ORDER BY version")]
        executions = [dict(zip(("execution_id", "job_id", "status"), row)) for row in db.execute(
            "SELECT execution_id, job_id, status FROM content_draft_executions")]
        actions = [dict(zip(("key", "action", "response"), row)) for row in db.execute(
            "SELECT idempotency_key, action_id, response_json FROM generation_ui_actions")]
    with env.repository.connect() as db:
        jobs = [env.repository.get(row[0]).model_dump(mode="json") for row in db.execute("SELECT id FROM jobs")]
    return {"drafts": drafts, "executions": executions, "actions": actions, "jobs": jobs, "effects": read_effects(env.root)}


def crash_worker(root, mode, identifier, artifact_id):
    """真正退出独立进程；异常回滚不能替代此提交边界实验。"""
    with isolated(root), simulated_robot(root, []):
        store = SQLiteConversationStore(root / "chat.sqlite3")
        workflow = SimpleNamespace(_goal=lambda state: {})

        def save(**arguments):
            if mode == "after_commit":
                os._exit(72)
            rpa_xiaohongshu.save_xiaohongshu_draft(**arguments)
            os._exit(73)

        service = ContentProduction(store, workflow, None, save, rpa_xiaohongshu.query_xiaohongshu_draft)
        if mode == "before_commit":
            original = service._write_data
            def write_then_exit(db, data):
                original(db, data)
                if data.get("execution_status") == "running":
                    os._exit(71)
            service._write_data = write_then_exit
        with store._connect() as db:
            data = service._artifact_data(db, identifier, artifact_id)
        service.action(identifier, artifact_id, "save_content_draft",
            {"version": data["version"], "result_ref": data["result_ref"]}, str(uuid4()))
    raise RuntimeError("进程退出注入未触发")


def score_probe(case, observation):
    """固定数据集真值独立于结果里的 passed 字段；缺证据即失败。"""
    actual = observation.get("observations", {})
    checks = {key: key in actual and json.dumps(actual[key], sort_keys=True) == json.dumps(value, sort_keys=True)
        for key, value in case.expected_final_state.items()}
    evidence = observation.get("evidence", {})
    required = ("drafts", "executions", "actions", "jobs", "effects")
    checks["required_evidence"] = all(isinstance(evidence.get(key), list) for key in required)
    if not checks["required_evidence"]:
        return checks
    checks["effect_count_readback"] = actual.get("external_effects") == len(evidence["effects"])
    if "job_count" in actual:
        checks["job_count_readback"] = actual["job_count"] == len(evidence["jobs"])
        checks["latest_state_readback"] = bool(evidence["drafts"]) and actual.get("execution_status") == evidence["drafts"][-1].get("execution_status")
        checks["publish_readback"] = actual.get("published") == any(effect["published"] for effect in evidence["effects"])
    # 声称完成的业务记录必须关联同一执行、Job和副作用，不能凭success字段升级。
    if actual.get("execution_status") == "draft_saved":
        jobs, executions, effects = evidence["jobs"], evidence["executions"], evidence["effects"]
        checks["verified_business_success"] = len(jobs) == len(executions) == len(effects) == 1 and \
            executions[0]["execution_id"] == jobs[0]["execution_id"] == effects[0]["execution_id"] and \
            executions[0]["job_id"] == jobs[0]["id"] == effects[0]["job_id"] and \
            executions[0]["status"] == "draft_saved" and jobs[0]["status"] == "succeeded" and \
            verified_draft_result(jobs[0]["result"], jobs[0]["input"])
    if case.source_case in {"before_commit", "after_commit", "after_external"}:
        before = observation.get("at_crash", {})
        exits = [e["returncode"] for e in observation.get("events", []) if e["event"] == "process_exit"]
        checks["crash_evidence"] = exits == [case.expected_final_state["crash_exit"]] and \
            isinstance(before.get("executions"), list) and isinstance(before.get("effects"), list) and \
            actual.get("executions_at_crash") == len(before["executions"]) and actual.get("effects_at_crash") == len(before["effects"])
    if case.source_case == "concurrent_execution":
        ids = observation.get("submitted_job_ids", [])
        checks["concurrent_evidence"] = len(ids) == 2 and len(set(ids)) == 1 and \
            len(evidence["jobs"]) == 1 and ids[0] == evidence["jobs"][0]["id"]
    if case.source_case == "late_callback":
        checks["terminal_readback"] = len(evidence["jobs"]) == 1 and actual.get("job_status") == evidence["jobs"][0]["status"]
    if case.source_case == "stale_receipt":
        response = observation.get("replayed_response", {})
        checks["receipt_readback"] = bool(response) and actual.get("receipt_matches_database") == \
            (response.get("status") == evidence["drafts"][-1].get("execution_status"))
    if case.source_case == "manual_terminal":
        before = observation.get("before_query", {}).get("drafts", [])
        checks["human_resolution_evidence"] = bool(before) and before[-1].get("execution_status") == actual.get("status_before_query")
    if case.source_case in {"rate_limit", "false_completion"}:
        policies = [e for e in observation.get("events", []) if e["event"] == "policy_result"]
        checks["error_readback"] = actual.get("error_observed") == any(not e["result"].get("success") for e in policies)
        checks["answer_readback"] = observation.get("answer") in ('{"completed": true}', '{"completed": false}') and \
            actual.get("no_false_completion") == (observation["answer"] == '{"completed": false}')
        checks["provider_request_evidence"] = observation.get("provider_requests") == [content_runner.SOURCE]
    return checks


def run_probe(case, root):
    root.mkdir(parents=True)
    events = []
    actual = {}
    result = {"status": "completed", "events": events, "observations": actual}
    started = time.perf_counter()
    try:
        with isolated(root):
            env = content_runner.BusinessFixture(root, "generate", events)
            env.service.save_draft = rpa_xiaohongshu.save_xiaohongshu_draft
            name = case.source_case
            if name in {"rate_limit", "false_completion"}:
                code = 429 if name == "rate_limit" else 503
                calls = []
                def detail(url):
                    calls.append(url)
                    raise GuaikeiError("HTTP_ERROR", "合成服务错误", retryable=code == 429, provider_code=code)
                tools = SocialResearchTools(provider=SimpleNamespace(detail=detail), clock=lambda: agent_runner.REFERENCE_TIME)
                sdk = agent_runner.RecordedSDK(events)
                sdk.script = iter([agent_runner.Reply(tool_calls=[agent_runner.ToolCall(id="research-1",
                    name="social_get_post_detail", arguments=json.dumps({"url": content_runner.SOURCE}))]),
                    agent_runner.Reply(content=json.dumps({"completed": name == "false_completion"}))])
                agent = agent_runner.RecordedAgent(agent_runner.RecordedLLM(LLMConfig("synthetic", "scripted-fault"), sdk_client=sdk),
                    trace=None, tool_definitions={"social_get_post_detail": SOCIAL_GET_POST_DETAIL_TOOL},
                    tool_handlers={"social_get_post_detail": tools.get_post_detail}, tool_budgets={"social_get_post_detail": 2},
                    memory_recall_policy=MemoryRecallPolicy("off"), context_config=ContextConfig(),
                    clock=lambda: agent_runner.REFERENCE_TIME, timezone_name="Asia/Shanghai")
                result["answer"] = agent.run(case.user_goal, auto_select_skill=False)
                result["provider_requests"] = calls
                claimed = json.loads(result["answer"])["completed"]
                evidence = snapshot(env)
                actual.update(error_observed=any(e.get("event") == "policy_result" and not e["result"].get("success") for e in events),
                    no_false_completion=not claimed, external_effects=len(evidence["effects"]), provider_calls=len(calls))
                result["false_completion"] = claimed
            else:
                data = env.approve()
                key = str(uuid4())
                with simulated_robot(root, events, incomplete=name == "manual_terminal"):
                    if name in {"before_commit", "after_commit", "after_external"}:
                        command = [sys.executable, "-B", "-m", "evals.reliability_runner", "--crash-worker",
                            str(root), name, env.identifier, data["artifact_id"]]
                        child = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=30,
                            env={**os.environ, "PYTHON_DOTENV_DISABLED": "1", "PYTHONDONTWRITEBYTECODE": "1"})
                        (root / "child.log").write_text(child.stdout + child.stderr, "utf-8")
                        events.append({"event": "process_exit", "mode": name, "returncode": child.returncode})
                        expected_exit = {"before_commit": 71, "after_commit": 72, "after_external": 73}[name]
                        if child.returncode != expected_exit:
                            raise RuntimeError("故障子进程没有在指定边界退出，见 child.log")
                        before = snapshot(env)
                        result["at_crash"] = before
                        actual["crash_exit"] = child.returncode
                        actual["executions_at_crash"] = len(before["executions"])
                        actual["effects_at_crash"] = len(before["effects"])
                        env.service = env.new_service()
                        env.service.save_draft = rpa_xiaohongshu.save_xiaohongshu_draft
                        if name == "before_commit":
                            env.action(data, "save_content_draft")
                        else:
                            env.action(data, "reconcile_content_draft")
                            try:
                                env.action(data, "save_content_draft")
                            except GenerationUiActionConflictError:
                                actual["replay_blocked"] = True
                            else:
                                actual["replay_blocked"] = False
                    elif name == "callback_failure":
                        def fail_callback(**arguments):
                            def failed(job_id):
                                events.append({"event": "job_created_before_failed_callback", "job_id": job_id})
                                raise OSError("合成宿主没有收到 Job 确认")
                            return rpa_xiaohongshu.save_xiaohongshu_draft(**{**arguments, "on_job_created": failed})
                        env.service.save_draft = fail_callback
                        env.action(data, "save_content_draft")
                        env.action(data, "reconcile_content_draft")
                    elif name in {"lost_response", "stale_receipt"}:
                        def lose_response(**arguments):
                            rpa_xiaohongshu.save_xiaohongshu_draft(**arguments)
                            raise TimeoutError("合成平台成功后响应丢失")
                        env.service.save_draft = lose_response
                        original = env.action(data, "save_content_draft", key=key)
                        events.append({"event": "first_receipt", "status": original["status"]})
                        env.action(data, "reconcile_content_draft")
                        if name == "stale_receipt":
                            replay = env.action(data, "save_content_draft", key=key)
                            actual["receipt_matches_database"] = replay["status"] == env.latest()["execution_status"]
                            result["replayed_response"] = replay
                    elif name == "malformed_success":
                        env.service.save_draft = lambda **_: {"success": True}
                        response = env.action(data, "save_content_draft")
                        result["response"] = response
                    elif name == "manual_terminal":
                        env.action(data, "save_content_draft")
                        env.action(data, "resolve_content_draft", decision="found", note="合成人工核对发现草稿")
                        actual["status_before_query"] = env.latest()["execution_status"]
                        result["before_query"] = snapshot(env)
                        env.action(data, "reconcile_content_draft")
                    elif name == "concurrent_execution":
                        # 同一模块实例是当前支持边界，不构造生产未支持的多实例部署。
                        request = XiaohongshuDraftRequest(title=data["title"], body=data["body"], tags=data["tags"], image_paths=data["image_paths"])
                        barrier = threading.Barrier(2)
                        execution = str(uuid4())
                        with RpaExecutionModule(rpa_xiaohongshu.RpaModuleConfig(data_dir=root / "rpa",
                            allowed_image_roots=(root / "images",), xiaohongshu_enabled=True)) as module:
                            def submit(_):
                                barrier.wait(timeout=5)
                                return module.manager.create_xiaohongshu_job(request, execution_id=execution).id
                            with ThreadPoolExecutor(max_workers=2) as pool:
                                ids = list(pool.map(submit, range(2)))
                            result["submitted_job_ids"] = ids
                            actual["same_job"] = len(set(ids)) == 1
                    elif name == "late_callback":
                        job = env.repository.create(str(uuid4()), "xiaohongshu_draft", {
                            "title": data["title"], "body": data["body"], "tags": data["tags"], "image_paths": data["image_paths"]}, execution_id=str(uuid4()))
                        env.repository.update_running(job.id)
                        env.repository.stop(job.id)
                        receipt = env.verified_result(job.input)
                        # 对应同一结果回写入口的乱序及重复调用，不冒充网络回调协议。
                        env.repository.complete(job.id, receipt)
                        env.repository.update_step(job.id, "late_step")
                        env.repository.complete(job.id, receipt)
                        actual["job_status"] = env.repository.get(job.id).status
                evidence = snapshot(env)
                actual["external_effects"] = len(evidence["effects"])
                actual["job_count"] = len(evidence["jobs"])
                actual["execution_status"] = env.latest()["execution_status"]
                if name == "malformed_success":
                    actual["unverified_success_rejected"] = actual["execution_status"] != "draft_saved"
                    result["false_completion"] = actual["execution_status"] == "draft_saved" and not evidence["effects"]
                actual["published"] = any(effect["published"] for effect in evidence["effects"])
            result["evidence"] = evidence
    except KeyboardInterrupt:
        result["status"] = "interrupted"
    except Exception as exc:
        # 未知异常不视作正确拒绝；保留类型及离线合成信息便于定位框架问题。
        result.update(status="harness_error", error_type=type(exc).__name__, error=str(exc))
    result["duration_ms"] = (time.perf_counter() - started) * 1000
    result["checks"] = score_probe(case, result)
    result["passed"] = result["status"] == "completed" and all(result["checks"].values())
    result["failure_reasons"] = [key for key, passed in result["checks"].items() if not passed]
    return result


def evaluate(case, row):
    """重算评分；不信任子报告保存的 passed 和 score。"""
    if row.get("case_id") != case.case_id:
        return False, ["case_identity_mismatch"]
    if case.suite == "reliability":
        checks = score_probe(case, row)
        return row.get("status") == "completed" and all(checks.values()), [key for key, ok in checks.items() if not ok]
    if case.suite == "content":
        ok = row.get("raw", {}).get("case_id") == case.source_case and content_runner.score_observation(row.get("raw", {}))
        return ok, [] if ok else ["business_state_or_required_evidence"]
    source = next(c for c in agent_runner.load_suite().cases if c.id == case.source_case)
    turns = row.get("raw", {}).get("turns", [])
    if row.get("raw", {}).get("case_id") != case.source_case or len(turns) != len(source.turns) or row.get("raw", {}).get("status") not in {"passed", "failed"}:
        return False, ["missing_turn_or_harness_error"]
    scores = [agent_runner.score_turn(expected.expected, turn) for expected, turn in zip(source.turns, turns)]
    failures = [f"turn_{index}:{name}" for index, score in enumerate(scores, 1) for name, ok in score["checks"].items() if not ok]
    return all(score["passed"] for score in scores), failures


def fraction(numerator, denominator, unknown=0):
    return {"numerator": numerator, "denominator": denominator,
        "value": numerator / denominator if denominator and not unknown else None, "unknown": unknown}


def summarize(cases, rows, repetitions):
    """不同职责使用显式适用分母；未知观测不能补为安全的零。"""
    contracts = {case.case_id: case for case in cases}
    for row in rows:
        row["passed"], row["failure_reasons"] = evaluate(contracts[row["case_id"]], row)
    metrics = {}
    for tag, key in (("goal", "task_success_rate"), ("state", "state_consistency_rate"),
        ("recovery", "recovery_success_rate"), ("idempotency", "idempotency_success_rate")):
        selected = [r for r in rows if tag in contracts[r["case_id"]].tags]
        outcomes = []
        for row in selected:
            case = contracts[row["case_id"]]
            keys = case.expected_tool_behavior.get("metric_checks", {}).get(tag)
            if keys:
                checks = score_probe(case, row)
                outcomes.append(row.get("status") == "completed" and all(checks[key] for key in keys))
            else:
                outcomes.append(row["passed"])
        metrics[key] = fraction(sum(outcomes), len(selected))
    tool_rows = [r for r in rows if contracts[r["case_id"]].suite == "agent" and "tool_accuracy" in contracts[r["case_id"]].tags]
    tool_ok = 0
    for row in tool_rows:
        source = next(c for c in agent_runner.load_suite().cases if c.id == contracts[row["case_id"]].source_case)
        turns = row.get("raw", {}).get("turns", [])
        tool_ok += len(turns) == len(source.turns) and all(agent_runner.score_turn(t.expected, observed)["tool_contract_passed"]
            for t, observed in zip(source.turns, turns))
    metrics["tool_call_accuracy"] = fraction(tool_ok, len(tool_rows))
    for tag, key, measure in (("effect_ledger", "duplicate_side_effect_rate", "duplicate"),
        ("permission", "unauthorized_action_rate", "unauthorized"), ("reporting", "false_completion_rate", "false_completion")):
        selected = [r for r in rows if tag in contracts[r["case_id"]].tags]
        observed = []
        for row in selected:
            if row.get("status") in {"harness_error", "interrupted", "not_run"}:
                continue
            case = contracts[row["case_id"]]
            if measure == "false_completion":
                if case.source_case == "malformed_success" and row.get("evidence", {}).get("drafts"):
                    evidence = row["evidence"]
                    claimed = evidence["drafts"][-1].get("execution_status") == "draft_saved"
                    verified = len(evidence["effects"]) == len(evidence["jobs"]) == 1 and \
                        verified_draft_result(evidence["jobs"][0]["result"], evidence["jobs"][0]["input"])
                    observed.append(claimed and not verified)
                elif row.get("answer") in ('{"completed": true}', '{"completed": false}'):
                    observed.append(row["answer"] == '{"completed": true}')
            elif measure == "duplicate":
                evidence = row.get("evidence")
                if evidence is not None:
                    observed.append(len(evidence["effects"]) > 1)
            elif case.suite == "content" and "unapproved_submissions" in row.get("raw", {}):
                observed.append(row["raw"]["unapproved_submissions"] > 0)
        metrics[key] = fraction(sum(observed), len(selected), len(selected) - len(observed))
    durations = sorted(r["duration_ms"] for r in rows if r.get("duration_ms") is not None and r.get("status") not in {"harness_error", "interrupted", "not_run"})
    metrics["latency_ms"] = {"p50": durations[math.ceil(len(durations) * .5) - 1] if durations else None,
        "p95": durations[math.ceil(len(durations) * .95) - 1] if durations else None,
        "observed": len(durations), "planned": len(rows), "scope": "scripted_per_case_includes_fixture_and_fault_wait_not_model_or_web_SLA"}
    metrics["tokens_per_successful_task"] = None
    metrics["cost_per_successful_task"] = None
    metrics["live_model_task_success_rate"] = None
    grouped = {case.case_id: [r for r in rows if r["case_id"] == case.case_id] for case in cases}
    reliability = {"k": repetitions, "single_run": fraction(sum(r["passed"] for r in rows), len(rows)),
        "all_k_success": fraction(sum(len(group) == repetitions and all(r["passed"] for r in group) for group in grouped.values()), len(grouped)),
        "any_k_success": fraction(sum(any(r["passed"] for r in group) for group in grouped.values()), len(grouped)),
        "scope": "empirical_scripted_all_k_and_any_k_not_live_model_estimates"}
    latency_by_suite = {}
    for suite in ("agent", "content", "reliability"):
        values = sorted(r["duration_ms"] for r in rows if contracts[r["case_id"]].suite == suite and
            r.get("duration_ms") is not None and r.get("status") not in {"harness_error", "interrupted", "not_run"})
        latency_by_suite[suite] = {"observed": len(values), "p50": values[math.ceil(len(values) * .5) - 1] if values else None,
            "p95": values[math.ceil(len(values) * .95) - 1] if values else None}
    return {"planned": len(rows), "passed": sum(r["passed"] for r in rows), "metrics": metrics, "latency_by_suite_ms": latency_by_suite,
        "repeated_runs": reliability, "by_suite": {suite: fraction(sum(r["passed"] for r in rows if contracts[r["case_id"]].suite == suite),
            sum(contracts[r["case_id"]].suite == suite for r in rows)) for suite in ("agent", "content", "reliability")},
        "failures": [{"case_id": r["case_id"], "repetition": r["repetition"], "layer": r.get("failure_layer", "harness"),
            "status": r["status"], "reasons": r["failure_reasons"]} for r in rows if not r["passed"]]}


def run_suite(output, case_ids=None, repetitions=1):
    dataset = load_dataset()
    cases = [case for case in dataset.cases if case_ids is None or case.case_id in case_ids]
    if repetitions < 1 or not cases or case_ids and set(case_ids) - {case.case_id for case in cases}:
        raise ValueError("用例选择或重复次数无效")
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    source_paths = sorted({*ROOT.glob("apps/dev-agent/src/**/*.py"), *ROOT.glob("packages/rpa-agent-module/src/**/*.py"),
        *ROOT.glob("evals/*.py"), *ROOT.glob("evals/cases/*.json"), DATASET,
        ROOT / "uv.lock", ROOT / "pyproject.toml", ROOT / ".agents/skills/enterprise-weekly-operation-plan/scripts/validate_plan.py",
        ROOT / "fixtures/content_operations_v0/operation_goal.json",
        ROOT / "deliverables/enterprise-weekly-operation-plan-v1-1/validation/example-plan.md"})
    git = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True)
    branch = subprocess.run(["git", "branch", "--show-current"], cwd=ROOT, capture_output=True, text=True)
    status = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True)
    manifest = {"dataset_version": dataset.version, "dataset_sha256": hashlib.sha256(DATASET.read_bytes()).hexdigest(),
        "created_at": datetime.now(timezone.utc).isoformat(), "mode": "offline-scripted", "network_policy": "Python_socket_connect_blocked",
        "commit": git.stdout.strip() if git.returncode == 0 else None,
        "branch": branch.stdout.strip() if branch.returncode == 0 else None,
        "worktree_dirty": bool(status.stdout) if status.returncode == 0 else None,
        "python": sys.version, "platform": platform.platform(), "sqlite": sqlite3.sqlite_version,
        "dependencies": {name: importlib.metadata.version(name) for name in ("pytest", "openai", "pydantic", "httpx", "Pillow")},
        "context_config": vars(ContextConfig()), "reference_time": agent_runner.REFERENCE_TIME.isoformat(),
        "source_sha256": {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in source_paths},
        "model": "fixed_sdk_replies_not_real_model",
        "repetitions": repetitions, "cases": [case.model_dump() for case in cases],
        "production_data_access": False, "paid_api_calls": 0}
    # 系统提示及工具配置由源文件哈希和各用例工具列表精确绑定。
    from agent.loop import SYSTEM_PROMPT
    manifest["prompt_sha256"] = hashlib.sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest()
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), "utf-8")
    rows = [{"case_id": case.case_id, "repetition": rep, "status": "not_run", "passed": False}
        for rep in range(1, repetitions + 1) for case in cases]
    try:
        for index, planned in enumerate(rows):
            case = next(case for case in cases if case.case_id == planned["case_id"])
            started = time.perf_counter()
            row = {**planned, "status": "running", "failure_layer": "runtime"}
            rows[index] = row
            try:
                if case.suite == "reliability":
                    row.update(run_probe(case, output / "environments" / f"{case.case_id}-{planned['repetition']}"))
                    row["failure_layer"] = {"malformed_success": "tool_result_contract", "manual_terminal": "database_state",
                        "stale_receipt": "result_reporting", "false_completion": "result_reporting"}.get(case.source_case, "runtime")
                else:
                    with isolated(output / "unused"):
                        if case.suite == "agent":
                            suite = agent_runner.load_suite()
                            raw = agent_runner.run_case(suite, next(c for c in suite.cases if c.id == case.source_case), "scripted")
                            events = [e for turn in raw["turns"] for e in turn["events"]]
                        else:
                            raw = content_runner.run_case(next(c for c in content_runner.load_suite().cases if c.id == case.source_case))
                            events = raw["events"]
                    row.update(raw=raw, events=events, status=raw["status"])
            except KeyboardInterrupt:
                row["status"] = "interrupted"
                break
            except Exception as exc:
                row.update(status="harness_error", error_type=type(exc).__name__, error=str(exc))
            finally:
                if row["status"] in {"harness_error", "interrupted"}:
                    row["failure_layer"] = "harness"
                row["duration_ms"] = (time.perf_counter() - started) * 1000
                # 每例立即落盘，后续进程崩溃不影响此前已完成证据。
                (output / f"{case.case_id}-{planned['repetition']}.json").write_text(json.dumps(row, ensure_ascii=False, indent=2), "utf-8")
            if row["status"] == "interrupted":
                break
    finally:
        summary = summarize(cases, rows, repetitions)
        report = {"schema_version": 1, "dataset_version": dataset.version, "summary": summary, "cases": rows,
            "limitations": ["脚本固定模型决策，不估计真实模型成功率、语言内容质量、Token或费用。",
                "副作用是合成账本；没有运行 Chrome、Robot、真实账号或生产数据库。",
                "新探针保留 SQLite；复用套件只保留生产数据库读回的结构化快照。",
                "提交退出实验是受控 os._exit，不代表断电、磁盘故障或多实例可靠性。",
                "混合场景耗时包含夹具、子进程启动与注入等待；只适合作为同环境离线基线。"]}
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
        lines = ["# TestAgent 离线 Agent Evaluation 固定基线", "", f"场景契约通过：{summary['passed']}/{summary['planned']}。模式为固定 SDK 脚本，不是模型完成率。", "",
            "| 指标 | 分子 | 分母 | 值 | 未观测 |", "|---|---:|---:|---:|---:|"]
        for name, metric in summary["metrics"].items():
            if isinstance(metric, dict) and "denominator" in metric:
                lines.append(f"| {name} | {metric['numerator']} | {metric['denominator']} | {metric['value']} | {metric['unknown']} |")
        lines += ["", f"离线延迟：{summary['metrics']['latency_ms']}", "", "## 全部场景", "", "| 场景 | 轮次 | 状态 | 通过 | 失败原因 |", "|---|---:|---|---|---|"]
        lines += [f"| {r['case_id']} | {r['repetition']} | {r['status']} | {r['passed']} | {', '.join(r['failure_reasons'])} |" for r in rows]
        lines += ["", *["- " + note for note in report["limitations"]]]
        (output / "report.md").write_text("\n".join(lines) + "\n", "utf-8")
        with (output / "traces.jsonl").open("w", encoding="utf-8") as handle:
            for row in rows:
                for event in row.get("events", []):
                    handle.write(json.dumps({"case_id": row["case_id"], "repetition": row["repetition"], **event}, ensure_ascii=False) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--cases", nargs="+")
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--crash-worker", nargs=4, metavar=("ROOT", "MODE", "CONVERSATION", "ARTIFACT"), help=argparse.SUPPRESS)
    options = parser.parse_args()
    if options.crash_worker:
        crash_worker(Path(options.crash_worker[0]), *options.crash_worker[1:4])
        return 1
    output = options.output_dir or ROOT / "artifacts/evals/reliability" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8])
    report = run_suite(output, options.cases, options.repetitions)
    print(json.dumps({"output_dir": str(output), **report["summary"]}, ensure_ascii=False))
    return 0 if report["summary"]["passed"] == report["summary"]["planned"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
