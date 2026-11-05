"""Agent 工具链评测：脚本回放与真实模型分别归档，复用现有执行逻辑。"""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
from pathlib import Path, PurePosixPath
import statistics
import subprocess
import tempfile
import time
from types import SimpleNamespace
from typing import Annotated, Any, Literal
from uuid import uuid4

import httpx
from openai import APITimeoutError, OpenAI
from openai.types.chat import ChatCompletionMessage
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from agent.context import ContextConfig, MemoryRecallPolicy, serialize_tool_result
from agent.loop import AgentLoop, READ_FILE_TOOL
from llm.client import LLMClient, LLMConfig, LLMConnectionError
from tools.read_file import read_file


ROOT = Path(__file__).resolve().parents[1]
CASE_PATH = ROOT / "evals/cases/agent_tool_cases.json"
REFERENCE_TIME = datetime(2026, 11, 3, 9, tzinfo=timezone.utc)


def _relative_file(value: str) -> str:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "\\" in value or str(path) == ".":
        raise ValueError("夹具文件必须使用工作区内的相对路径")
    return value


FileName = Annotated[str, AfterValidator(_relative_file)]


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolCall(InputModel):
    id: str
    name: str = "read_file"
    arguments: str


class Reply(InputModel):
    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    timeout: bool = False


class Expected(InputModel):
    answer: str | dict[str, Any] | None = None
    required_files: list[str] = Field(default_factory=list)
    allowed_paths: list[str] | None = None
    handler_paths: list[str] | None = None
    handler_count: int | None = None
    forbidden_text: list[str] = Field(default_factory=list)
    request_count: int | None = None
    policy_errors: list[str | None] | None = None
    policy_statuses: list[str | None] | None = None
    model_rounds: int | None = None
    sdk_attempts: int | None = None
    terminal: str | None = None
    terminal_contains: str = ""

    @model_validator(mode="after")
    def evidence_contract(self) -> "Expected":
        if self.required_files and (not isinstance(self.answer, dict)
                                   or sorted(self.answer.get("sources", [])) != sorted(self.required_files)):
            raise ValueError("需要读取的文件必须同时列入预期答案来源")
        return self


class Turn(InputModel):
    task: str
    updates: dict[FileName, str] = Field(default_factory=dict)
    script: list[Reply]
    expected: Expected


class Case(InputModel):
    id: str = Field(pattern=r"^[AB][0-9]{2}$")
    description: str
    kind: Literal["normal", "expected_failure", "runtime"]
    live: bool = False
    max_steps: int = Field(default=5, ge=1, le=10)
    budget: int = Field(default=5, ge=1, le=10)
    fault: Literal["tool_timeout"] | None = None
    turns: list[Turn] = Field(min_length=1)


class Suite(InputModel):
    schema_version: Literal[1]
    files: dict[FileName, str]
    outside_text: str
    cases: list[Case] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_cases(self) -> "Suite":
        if len({case.id for case in self.cases}) != len(self.cases):
            raise ValueError("用例 ID 不能重复")
        if any(case.live and (case.fault or case.kind == "runtime") for case in self.cases):
            raise ValueError("故障注入用例不能进入真实模型基线")
        return self


def load_suite(path: Path = CASE_PATH) -> Suite:
    """用例文件在入口一次性解析；故意损坏的工具参数保留为原始字符串。"""
    return Suite.model_validate_json(path.read_text("utf-8"))


class ScriptExhausted(RuntimeError):
    """脚本耗尽属于评测器错误，不是被测 Agent 的正确终止。"""


class RecordedSDK:
    """在 SDK 边界记录响应、重试和用量；脚本模式不会构造网络客户端。"""

    def __init__(self, events: list[dict[str, Any]], delegate: Any = None) -> None:
        self.chat = self.completions = self
        self.events = events
        self.delegate = delegate
        self.script = iter(())
        self.turn = 0
        self.round = 0
        self.pending_calls: list[dict[str, Any]] = []

    def create(self, **request: Any) -> Any:
        event = {"event": "sdk_attempt", "turn": self.turn, "round": self.round,
                 "request": deepcopy(request), "usage": None}
        self.events.append(event)
        started = time.perf_counter()
        try:
            if self.delegate is not None:
                response = self.delegate.chat.completions.create(**request)
            else:
                reply = next(self.script, None)
                if reply is None:
                    raise ScriptExhausted("固定响应已耗尽")
                if reply.timeout:
                    raise APITimeoutError(request=httpx.Request("POST", "https://eval.invalid"))
                message = ChatCompletionMessage(
                    role="assistant", content=reply.content,
                    tool_calls=[{"id": call.id, "type": "function", "function": {
                        "name": call.name, "arguments": call.arguments}} for call in reply.tool_calls],
                )
                response = SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)
            event["response"] = response.choices[0].message.model_dump(exclude_none=True)
            if response.usage is not None:
                event["usage"] = response.usage.model_dump(mode="json")
            return response
        except KeyboardInterrupt:
            event["error_type"] = "KeyboardInterrupt"
            raise
        except Exception as exc:
            # SDK 异常正文可能带认证信息，只归档类型。
            event["error_type"] = type(exc).__name__
            raise
        finally:
            event["duration_ms"] = (time.perf_counter() - started) * 1000


class RecordedLLM(LLMClient):
    def complete(self, messages: Any, tools: Any = None) -> Any:
        sdk = self._client
        sdk.round += 1
        sdk.events.append({"event": "model_round", "turn": sdk.turn, "round": sdk.round})
        message = super().complete(messages, tools)
        sdk.pending_calls = deepcopy(message.model_dump(exclude_none=True).get("tool_calls", []))
        return message


class RecordedAgent(AgentLoop):
    def _execute_tool_with_policy(self, name: str, raw_arguments: str, **options: Any) -> dict[str, Any]:
        sdk = self.client._client
        call = sdk.pending_calls.pop(0)
        sdk.active_call_id = call["id"]
        event = {"event": "policy_result", "turn": sdk.turn, "round": sdk.round,
                 "call_id": call["id"], "name": name, "arguments": raw_arguments,
                 "result": {}, "completed": False}
        sdk.events.append(event)
        result = super()._execute_tool_with_policy(name, raw_arguments, **options)
        event.update(result=deepcopy(result), completed=True)
        return result


def score_turn(expected: Expected, observation: dict[str, Any]) -> dict[str, Any]:
    """按任务真值及实际轨迹判分，不把工具成功或任意异常直接当作通过。"""
    events = observation["events"]
    policies = [e for e in events if e["event"] == "policy_result"]
    handlers = [e for e in events if e["event"] == "handler_execution"]
    attempts = [e for e in events if e["event"] == "sdk_attempt"]
    successful_files = {e["result"]["path"] for e in handlers if e.get("result", {}).get("success") is True}
    # 原文继续保留在事件中；判分复用一次解析后的参数，忽略 JSON 排版差异。
    arguments = []
    for event in policies:
        try:
            arguments.append(json.loads(event["arguments"]))
        except ValueError:
            arguments.append(None)
    checks: dict[str, bool] = {}
    error = observation.get("error") or {}
    checks["terminal"] = (error.get("type") == expected.terminal
                          and expected.terminal_contains in (error.get("message") or ""))
    actual = observation.get("answer")
    correct_fields = total_fields = 0
    source_passed: bool | None = None
    if isinstance(expected.answer, dict):
        try:
            actual = json.loads(actual or "")
        except ValueError:
            actual = None
        checks["answer_shape"] = isinstance(actual, dict) and actual.keys() == expected.answer.keys()
        for field, value in expected.answer.items():
            got = actual.get(field) if isinstance(actual, dict) else None
            if field == "sources":
                valid_sources = (isinstance(got, list) and all(isinstance(x, str) for x in got)
                                 and sorted(got) == sorted(value) and set(got) <= successful_files)
                checks["sources"] = valid_sources
                source_passed = valid_sources if value else None
            else:
                same = (type(got) in (int, float) and got == value) if type(value) in (int, float) else type(got) is type(value) and got == value
                checks[f"answer.{field}"] = same
                total_fields += 1
                correct_fields += int(same)
    else:
        checks["answer"] = (actual.strip() if isinstance(actual, str) else actual) == expected.answer

    checks["required_files"] = set(expected.required_files) <= successful_files
    if expected.allowed_paths is not None:
        allowed = True
        for event, args in zip(policies, arguments):
            if not isinstance(args, dict):
                allowed = False
                continue
            path = event["result"].get("path", args.get("path") if isinstance(args, dict) else None)
            if isinstance(path, str):
                path = str(PurePosixPath(path)).removeprefix("workspace/")
            allowed &= event["name"] == "read_file" and path in expected.allowed_paths
        checks["allowed_requests"] = bool(allowed)
    if expected.handler_paths is not None:
        checks["handler_paths"] = [e["path"] for e in handlers] == expected.handler_paths
    if expected.handler_count is not None:
        checks["handler_count"] = len(handlers) == expected.handler_count
    for index, text in enumerate(expected.forbidden_text):
        checks[f"forbidden_text.{index}"] = text not in json.dumps(observation, ensure_ascii=False)
    if expected.request_count is not None:
        checks["request_count"] = len(policies) == expected.request_count
    if expected.policy_errors is not None:
        checks["policy_errors"] = [e["result"].get("error_type") for e in policies] == expected.policy_errors
    if expected.policy_statuses is not None:
        checks["policy_statuses"] = [e["result"].get("_agent_execution", {}).get("status") for e in policies] == expected.policy_statuses
    for key, count in (("model_rounds", sum(e["event"] == "model_round" for e in events)), ("sdk_attempts", len(attempts))):
        wanted = getattr(expected, key)
        if wanted is not None:
            checks[key] = count == wanted

    # 只有发生后续模型调用时，才声称结果已经实际回填到模型请求。
    for index, event in enumerate(policies):
        responses = [e["response"] for e in attempts if e["round"] == event["round"] and "response" in e]
        requested = [call for response in responses for call in response.get("tool_calls", [])
                     if call["id"] == event["call_id"]]
        checks[f"request_binding.{index}"] = len(requested) == 1 and requested[0]["function"] == {
            "name": event["name"], "arguments": event["arguments"]}
        later = [e for e in attempts if e["round"] > event["round"]]
        if later:
            delivered = [m for m in later[0]["request"]["messages"]
                         if m.get("role") == "tool" and m.get("tool_call_id") == event["call_id"]]
            expected_content = json.loads(serialize_tool_result(event["result"], max_chars=8000))
            try:
                delivered_content = json.loads(delivered[0]["content"]) if len(delivered) == 1 else None
            except (ValueError, TypeError):
                delivered_content = None
            checks[f"backfill.{index}"] = len(delivered) == 1 and delivered_content == expected_content
        meta = event["result"].get("_agent_execution", {})
        if meta:
            used = sum(h["call_id"] in {p["call_id"] for p in policies[:index + 1]} for h in handlers)
            consumed = any(h["call_id"] == event["call_id"] for h in handlers)
            checks[f"budget_meta.{index}"] = (meta.get("used") == used and meta.get("limit") == observation["budget"]
                and meta.get("remaining") == max(observation["budget"] - used, 0)
                and meta.get("budget_consumed") is consumed)
        if meta.get("status") == "deduplicated":
            prior = [p for i, p in enumerate(policies[:index]) if p["name"] == event["name"]
                     and isinstance(arguments[index], dict) and arguments[i] == arguments[index]]
            body = {k: v for k, v in event["result"].items() if k != "_agent_execution"}
            previous = {k: v for k, v in prior[-1]["result"].items() if k != "_agent_execution"} if prior else None
            checks[f"dedupe.{index}"] = (body == previous and meta.get("budget_consumed") is False
                                         and not any(h["call_id"] == event["call_id"] for h in handlers))
    tool_checks = [value for key, value in checks.items() if not key.startswith("answer") and key != "sources"]
    return {"passed": all(checks.values()), "checks": checks,
            "tool_contract_passed": all(tool_checks), "correct_fields": correct_fields,
            "total_fields": total_fields, "source_passed": source_passed}


def run_case(suite: Suite, case: Case, mode: str, delegate: Any = None, config: LLMConfig | None = None,
             *, result: dict[str, Any] | None = None) -> dict[str, Any]:
    if (mode not in {"scripted", "live"} or (mode == "live") != (delegate is not None)
            or mode == "live" and config is None):
        raise ValueError("真实模式必须提供 SDK 和模型配置，脚本模式不能接收网络 SDK")
    events: list[dict[str, Any]] = []
    turns = []
    # 结果容器由调用方持有；用例尚未返回时也能在中断归档中保留当前轨迹。
    result = result if result is not None else {}
    result.update(case_id=case.id, description=case.description, kind=case.kind, mode=mode,
                  expected_fields=sum(len(t.expected.answer.keys() - {"sources"}) for t in case.turns if isinstance(t.expected.answer, dict)),
                  requires_sources=any(t.expected.required_files for t in case.turns),
                  status="running", duration_ms=0, turns=turns)
    sdk = RecordedSDK(events, delegate)
    with tempfile.TemporaryDirectory(prefix="agent-tool-eval-") as directory:
        workspace = Path(directory) / "workspace"
        workspace.mkdir()
        (Path(directory) / "outside.md").write_text(suite.outside_text, "utf-8")
        for name, content in suite.files.items():
            target = workspace / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, "utf-8")

        def tracked_read(path: str) -> dict[str, Any]:
            event = {"event": "handler_execution", "turn": sdk.turn, "round": sdk.round,
                     "call_id": sdk.active_call_id, "path": path}
            events.append(event)
            started = time.perf_counter()
            try:
                if case.fault == "tool_timeout":
                    raise TimeoutError("评测注入的工具超时")
                result = read_file(path, workspace=workspace)
                event["result"] = deepcopy(result)
                return result
            except KeyboardInterrupt:
                event["error_type"] = "KeyboardInterrupt"
                raise
            except Exception as exc:
                event["error_type"] = type(exc).__name__
                raise
            finally:
                event["duration_ms"] = (time.perf_counter() - started) * 1000

        client = RecordedLLM(config or LLMConfig("script-only", "scripted"), sdk_client=sdk)
        agent = RecordedAgent(client, trace=None, max_steps=case.max_steps,
                              tool_definitions={"read_file": READ_FILE_TOOL},
                              tool_handlers={"read_file": tracked_read}, tool_budgets={"read_file": case.budget},
                              memory=None, context_config=ContextConfig(), memory_recall_policy=MemoryRecallPolicy("off"),
                              clock=lambda: REFERENCE_TIME, timezone_name="Asia/Shanghai")
        history: list[dict[str, str]] = []
        for index, turn in enumerate(case.turns, 1):
            for name, content in turn.updates.items():
                target = workspace / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, "utf-8")
            sdk.turn, sdk.round = index, 0
            sdk.script = iter(turn.script)
            offset = len(events)
            observation: dict[str, Any] = {"answer": None, "error": None, "budget": case.budget,
                "score": {"passed": False, "checks": {"incomplete": False}, "tool_contract_passed": False,
                          "correct_fields": 0, "total_fields": len(turn.expected.answer.keys() - {"sources"}) if isinstance(turn.expected.answer, dict) else 0,
                          "source_passed": False if turn.expected.required_files else None}}
            turns.append(observation)
            started = time.perf_counter()
            try:
                observation["answer"] = agent.run(turn.task, history=history, auto_select_skill=False)
                history.extend([{"role": "user", "content": turn.task}, {"role": "assistant", "content": observation["answer"]}])
            except KeyboardInterrupt:
                observation["error"] = {"type": "KeyboardInterrupt", "message": None}
                result["status"] = "interrupted"
                raise
            except Exception as exc:
                observation["error"] = {"type": type(exc).__name__,
                                        "message": str(exc) if type(exc) in (RuntimeError, LLMConnectionError) else None}
            finally:
                observation["duration_ms"] = (time.perf_counter() - started) * 1000
                observation["events"] = deepcopy(events[offset:])
                result["duration_ms"] = sum(t["duration_ms"] for t in turns)
            observation["score"] = score_turn(turn.expected, observation)
    harness_error = any((turn["error"] or {}).get("type") == "ScriptExhausted" for turn in turns)
    result["status"] = "harness_error" if harness_error else "passed" if all(t["score"]["passed"] for t in turns) else "failed"
    return result


def aggregate_agent_results(results: list[dict[str, Any]], mode: str) -> dict[str, Any]:
    if any(row["mode"] != mode for row in results):
        raise ValueError("脚本与真实模型结果不能混合聚合")
    def rate(values: list[bool]) -> float | None:
        return sum(values) / len(values) if values else None

    scores = [t["score"] for r in results for t in r.get("turns", [])]
    normal = [r for r in results if r["kind"] == "normal"]
    abnormal = [r for r in results if r["kind"] != "normal"]
    events = [e for r in results for t in r.get("turns", []) for e in t["events"]]
    attempts = [e for e in events if e["event"] == "sdk_attempt"]
    usages = [e["usage"] for e in attempts if e.get("usage") is not None]
    policies = [e for e in events if e["event"] == "policy_result"]
    fields = sum(r["expected_fields"] for r in results)
    source_cases = [r for r in results if r["requires_sources"]]
    return {"planned": len(results), "passed": sum(r["status"] == "passed" for r in results),
            "statuses": {status: sum(r["status"] == status for r in results) for status in ("passed", "failed", "harness_error", "interrupted", "not_run")},
            "case_pass_rate": rate([r["status"] == "passed" for r in results]),
            "task_success_rate": rate([r["status"] == "passed" for r in normal]) if mode == "live" else None,
            "abnormal_case_pass_rate": rate([r["status"] == "passed" for r in abnormal]),
            "fact_field_accuracy": sum(s["correct_fields"] for s in scores) / fields if fields else None,
            "source_pass_rate": rate([r["status"] in {"passed", "failed"} and bool(r.get("turns")) and all(t["score"]["source_passed"] is True for t in r["turns"] if t["score"]["source_passed"] is not None) for r in source_cases]),
            "tool_contract_pass_rate": rate([r["status"] in {"passed", "failed"} and bool(r.get("turns")) and all(t["score"]["tool_contract_passed"] for t in r["turns"]) for r in results]),
            "model_rounds": sum(e["event"] == "model_round" for e in events), "sdk_attempts": len(attempts),
            "tool_requests": len(policies), "handler_executions": sum(e["event"] == "handler_execution" for e in events),
            "deduplications": sum(e["result"].get("_agent_execution", {}).get("status") == "deduplicated" for e in policies),
            "policy_rejections": sum(e["result"].get("error_type") in {"INVALID_ARGUMENTS", "UNKNOWN_TOOL", "TOOL_BUDGET_EXCEEDED"} for e in policies),
            "normal_case_durations_ms": [r["duration_ms"] for r in normal if r["status"] == "passed"] if mode == "live" else [],
            "normal_case_median_ms": statistics.median([r["duration_ms"] for r in normal if r["status"] == "passed"]) if mode == "live" and any(r["status"] == "passed" for r in normal) else None,
            "usage_observed_attempts": len(usages), "usage_missing_attempts": len(attempts) - len(usages),
            "observed_token_usage": {key: sum(u[key] for u in usages) for key in ("prompt_tokens", "completion_tokens", "total_tokens")} if usages else None,
            "cost_cny": None, "cost_basis": "未提供价格及账单口径；已观测用量不代表全部费用"}


def run_evaluation(*, mode: str = "scripted", case_path: Path = CASE_PATH,
                   case_ids: list[str] | None = None, repetitions: int = 1,
                   report_root: Path = ROOT / "artifacts/evals/agent-tools") -> tuple[Path, dict[str, Any]]:
    if mode not in {"scripted", "live"} or not 1 <= repetitions <= 10:
        raise ValueError("模式必须为 scripted/live，重复次数必须为 1–10")
    suite = load_suite(case_path)
    selected = [c for c in suite.cases if (case_ids is None or c.id in case_ids) and (mode != "live" or c.live)]
    if not selected or case_ids and set(case_ids) != {c.id for c in selected}:
        raise ValueError("用例不存在，或所选用例不支持当前模式")
    config = LLMConfig.from_env() if mode == "live" else LLMConfig("script-only", "scripted")
    output = report_root / f"{mode}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}"
    output.mkdir(parents=True, exist_ok=False)
    sources = [Path(__file__), case_path, ROOT / "apps/dev-agent/src/agent/loop.py",
               ROOT / "apps/dev-agent/src/agent/context.py", ROOT / "apps/dev-agent/src/llm/client.py",
               ROOT / "apps/dev-agent/src/tools/read_file.py"]
    git = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=False)
    status = subprocess.run(["git", "status", "--porcelain", "--untracked-files=normal"], cwd=ROOT, capture_output=True, text=True, check=False)
    manifest = {"mode": mode, "model": config.model, "created_at": datetime.now(timezone.utc).isoformat(),
                "commit": git.stdout.strip() if git.returncode == 0 else None,
                "worktree_dirty": bool(status.stdout) if status.returncode == 0 else None,
                "source_sha256": {str(p.resolve()): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources},
                "versions": {name: importlib.metadata.version(name) for name in ("openai", "pydantic")},
                "reference_time": REFERENCE_TIME.isoformat(), "timezone": "Asia/Shanghai",
                "context_config": vars(ContextConfig()), "sdk_timeout_seconds": 30 if mode == "live" else None,
                "sdk_internal_retries": 0, "client_connection_attempts": 3,
                "cases": [{"id": c.id, "max_steps": c.max_steps, "budget": c.budget} for c in selected],
                "repetitions": repetitions, "tool_names": ["read_file"], "memory_enabled": False}
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", "utf-8")
    results = [{"case_id": c.id, "kind": c.kind, "mode": mode, "repetition": rep, "status": "not_run",
                "expected_fields": sum(len(t.expected.answer.keys() - {"sources"}) for t in c.turns if isinstance(t.expected.answer, dict)),
                "requires_sources": any(t.expected.required_files for t in c.turns)}
               for rep in range(1, repetitions + 1) for c in selected]
    delegate = None
    try:
        if mode == "live":
            delegate = OpenAI(api_key=config.api_key, base_url=config.base_url, timeout=30, max_retries=0)
        for index, row in enumerate(results):
            case = next(c for c in selected if c.id == row["case_id"])
            try:
                run_case(suite, case, mode, delegate, config, result=row)
            except KeyboardInterrupt:
                if row["status"] == "running":
                    row["status"] = "interrupted"
                raise
            except Exception as exc:
                row.update(status="harness_error", error_type=type(exc).__name__)
    finally:
        if delegate is not None:
            delegate.close()
        summary = aggregate_agent_results(results, mode)
        report = {"schema_version": 1, "manifest": manifest, "summary": summary, "results": results}
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", "utf-8")
        with (output / "events.jsonl").open("w", encoding="utf-8") as stream:
            for row in results:
                for turn in row.get("turns", []):
                    for event in turn["events"]:
                        stream.write(json.dumps({"case_id": row["case_id"], "repetition": row["repetition"], **event}, ensure_ascii=False) + "\n")
        lines = ["# Agent 工具链评测", "", f"模式：{mode}；通过 {summary['passed']}/{summary['planned']}。",
                 "", ("真实模型评测；以下结果仅适用于本批用例。费用未估算。" if mode == "live"
                       else "脚本回放验证执行链路和评分器，不代表真实模型质量。费用未估算。"), "",
                 "| 指标 | 观测值 |", "|---|---|"]
        for key in ("case_pass_rate", "task_success_rate", "abnormal_case_pass_rate", "fact_field_accuracy",
                    "source_pass_rate", "tool_contract_pass_rate", "model_rounds", "sdk_attempts", "tool_requests",
                    "handler_executions", "deduplications", "policy_rejections", "normal_case_median_ms",
                    "usage_observed_attempts", "usage_missing_attempts", "observed_token_usage", "cost_cny"):
            lines.append(f"| {key} | {json.dumps(summary[key], ensure_ascii=False)} |")
        lines.extend(["", ("null 表示未观测或不适用；费用未经账单核实。" if mode == "live"
                           else "null 表示未观测或不适用；脚本答案字段匹配率不是模型质量指标。"), "",
                      "| 用例 | 轮次 | 状态 | 未满足的验收条件 |", "|---|---|---|---|"])
        for row in results:
            failures = [f"turn{i}:{key}" for i, t in enumerate(row.get("turns", []), 1) for key, passed in t["score"]["checks"].items() if not passed]
            lines.append(f"| {row['case_id']} | {row['repetition']} | {row['status']} | {', '.join(failures) or row.get('error_type', '—')} |")
        (output / "report.md").write_text("\n".join(lines) + "\n", "utf-8")
    return output, report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("scripted", "live"), default="scripted")
    parser.add_argument("--cases", nargs="+", dest="case_ids")
    parser.add_argument("--case-file", type=Path, default=CASE_PATH)
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--report-root", type=Path, default=ROOT / "artifacts/evals/agent-tools")
    args = parser.parse_args()
    output, report = run_evaluation(mode=args.mode, case_path=args.case_file, case_ids=args.case_ids,
                                    repetitions=args.repetitions, report_root=args.report_root)
    print(f"{report['summary']['passed']}/{report['summary']['planned']} passed; report: {output}")
    raise SystemExit(0 if report["summary"]["passed"] == report["summary"]["planned"] else 1)


if __name__ == "__main__":
    main()
