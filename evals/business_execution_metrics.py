"""只读分析已有业务证据；不运行场景、不构造模型客户端、不调用平台。"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
import csv
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import subprocess
import xml.etree.ElementTree as ET
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCES = ROOT / "evals/business_execution_sources_v1.json"


class Source(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    kind: Literal["reliability", "robot", "live_content", "legacy"]
    path: str
    label: str
    model: Literal["scripted", "real", "unknown"]
    platform: Literal["simulated", "loopback", "forbidden_substitute", "real", "unknown"]
    executor: str
    data_origin: str
    rescore_v2: bool = False
    evidence_root: str | None = None


class Catalog(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1]
    description: str
    sources: list[Source] = Field(min_length=1)
    files_sha256: dict[str, str]


def read_json(path):
    return json.loads(Path(path).read_text("utf-8"))


def fingerprint(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_catalog(path=DEFAULT_SOURCES, root=ROOT):
    """来源只在入口定型、校验完整性；不默默吸收新批次或修改后的证据。"""
    catalog = Catalog.model_validate_json(Path(path).read_text("utf-8"))
    root = Path(root).resolve()
    if len({s.id for s in catalog.sources}) != len(catalog.sources):
        raise ValueError("来源编号重复")
    if len({s.path for s in catalog.sources}) != len(catalog.sources):
        raise ValueError("同一原始输入不能作为两个独立来源")
    required = {s.path for s in catalog.sources}
    for source in catalog.sources:
        if source.kind == "robot":
            if not source.evidence_root:
                raise ValueError("Robot来源缺少证据根目录")
            folder = (root / source.evidence_root).resolve()
            if not folder.is_relative_to(root):
                raise ValueError("证据目录超出项目根目录")
            required.update(str(p.relative_to(root)) for p in folder.rglob("*")
                            if p.is_file() and p.suffix in {".json", ".xml", ".db", ".sqlite3", ".log"})
    if not required <= catalog.files_sha256.keys():
        raise ValueError("证据清单不完整，需另行冻结来源版本")
    primary_hashes = [catalog.files_sha256[s.path] for s in catalog.sources]
    if len(set(primary_hashes)) != len(primary_hashes):
        raise ValueError("同一原始内容的副本不能重复计为独立来源")
    for name, expected in catalog.files_sha256.items():
        resolved = (root / name).resolve()
        if not resolved.is_relative_to(root) or not resolved.is_file():
            raise ValueError(f"证据不存在或超出项目：{name}")
        if fingerprint(resolved) != expected:
            raise ValueError(f"输入哈希变化，停止分析：{name}")
    return catalog


def query_rows(path, sql, parameters=()):
    """直接使用只读连接，避免业务仓储构造器建表、恢复任务或创建目录。"""
    with sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        db.row_factory = sqlite3.Row
        rows = [dict(r) for r in db.execute(sql, parameters)]
        db.rollback()
    return rows


def fraction(numerator, denominator, unknown=0):
    return {"numerator": numerator, "denominator": denominator, "unknown": unknown,
            "value": numerator / denominator if denominator and not unknown else None}


def latency_summary(values):
    observed = sorted(v for v in values if v is not None)
    return {"observed": len(observed), "missing": len(values) - len(observed),
            "p50_ms": observed[math.ceil(.5 * len(observed)) - 1] if observed else None,
            "p95_ms": observed[math.ceil(.95 * len(observed)) - 1] if observed else None}


def elapsed_ms(start, end):
    if not start or not end:
        return None
    value = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() * 1000
    return value if value >= 0 else None


def xml_readback(path):
    """从已运行关键字恢复读回区间；NOT RUN不表示执行或核验失败一次。"""
    if not path.is_file():
        return {"attempted": None, "duration_ms": None, "start": None, "end": None}
    task = ET.parse(path).getroot().find(".//test")
    parents = {child: parent for parent in task.iter() for child in parent}
    start = end = None
    # 任务通常只有一个外层业务关键字，读回标记位于其内部，不能只扫描直接子节点。
    for keyword in task.iter("kw"):
        status = keyword.find("status")
        if (keyword.get("name") == "Update Job Step" and keyword.findtext("arg") == "verifying_draft"
                and status is not None and status.get("status") == "PASS"):
            start = status.get("start")
            break
    for keyword in task.iter("kw"):
        status = keyword.find("status")
        if status is None or status.get("status") not in {"PASS", "FAIL"}:
            continue
        ancestor = keyword
        teardown = False
        while ancestor is not task:
            teardown |= ancestor.get("type") == "TEARDOWN"
            ancestor = parents[ancestor]
        if teardown:
            continue
        stamp = status.get("start")
        if start and stamp and stamp >= start and status.get("elapsed") is not None:
            candidate = (datetime.fromisoformat(stamp) + timedelta(seconds=float(status.get("elapsed")))).isoformat()
            end = max(end, candidate) if end else candidate
    return {"attempted": start is not None, "duration_ms": elapsed_ms(start, end), "start": start, "end": end}


def task_row(source, task_id, **fields):
    return {"source_id": source.id, "task_id": task_id, "source_path": source.path,
            "model": source.model, "platform": source.platform, "executor": source.executor,
            "data_origin": source.data_origin, "machine_verified": None,
            "automation_without_rescue": None, "human_rescue_performed": None,
            "human_resolution_recorded": None, "needs_human_review": None,
            "normal_approval_observed": None, "post_fault_trigger": "unknown", **fields}


def add_latency(latencies, row, stage, value, reason="", **extra):
    latencies.append({k: row.get(k) for k in ("source_id", "task_id", "source_path", "json_pointer",
                                             "model", "platform", "executor", "data_origin", "terminal", "contract_passed")}
                     | {"stage": stage, "duration_ms": value, "missing_reason": reason if value is None else "", **extra})


def summarize_tasks(rows):
    task_types = sorted({r["task_type"] for r in rows})
    # 混合目标或旧Action观察数不是业务任务总分母；只对单一明确目标提供分母。
    denominator = len(rows) if len(task_types) == 1 and task_types[0] != "legacy_save_action" else None
    save_rows = [r for r in rows if r["task_type"] in {"external_save", "external_save_probe"}]
    return {"tasks": len(rows), "terminal_counts": dict(Counter(r["terminal"] for r in rows)),
            "observed_failed": sum(r["terminal"] == "failed" for r in rows),
            "observed_cancelled": sum(r["terminal"] == "cancelled" for r in rows),
            "observed_unfinished": sum(r["terminal"] in {"running", "queued", "not_started", "not_run"} for r in rows),
            "machine_verified_results": sum(r.get("machine_verified") is True for r in rows),
            "denominator_note": "混合目标/旧Action总体分母为null；分目标观察数见by_task_type，均不代表生产总体。",
            "verified_save_results_per_observed_save_intent": fraction(sum(r["machine_verified"] is True for r in save_rows), len(save_rows),
                                                                      sum(r["machine_verified"] is None for r in save_rows)),
            "by_task_type": {kind: {"observations": sum(r["task_type"] == kind for r in rows),
                                    "terminal_counts": dict(Counter(r["terminal"] for r in rows if r["task_type"] == kind))}
                             for kind in task_types},
            "automatic_completion_rate": fraction(sum(r["automation_without_rescue"] is True for r in rows), denominator,
                                                   sum(r["automation_without_rescue"] is None for r in rows)),
            "abnormal_human_rescue_rate": fraction(sum(r["human_rescue_performed"] is True for r in rows), denominator,
                                                    sum(r["human_rescue_performed"] is None for r in rows)),
            "human_resolution_recorded": sum(r.get("human_resolution_recorded") is True for r in rows),
            "waiting_human_review": sum(r.get("needs_human_review") is True for r in rows),
            "normal_approval_observed": sum(r.get("normal_approval_observed") is True for r in rows)}


def summarize_jobs(rows):
    executions = defaultdict(set)
    for r in rows:
        if r["execution_id"]:
            executions[r["execution_id"]].add(r["job_id"])
    return {"jobs": len(rows), "status_counts": dict(Counter(r["job_status"] for r in rows)),
            "job_success_rate": fraction(sum(r["job_status"] == "succeeded" for r in rows), len(rows)),
            "verified_readback_per_created_job": fraction(sum(r["readback_verified"] is True for r in rows), len(rows),
                                                          sum(r["readback_verified"] is None for r in rows)),
            "readback_pass_rate_among_attempted": fraction(
                sum(r["readback_verified"] is True and r["readback_attempted"] is True for r in rows),
                sum(r["readback_attempted"] is True for r in rows),
                sum(r["readback_verified"] is None and r["readback_attempted"] is True for r in rows)),
            "readback_attempt_unknown": sum(r["readback_attempted"] is None for r in rows),
            "extra_jobs_per_execution": sum(max(len(ids) - 1, 0) for ids in executions.values()),
            "duplicate_external_effect_count": None,
            "duplicate_external_effect_note": "Job唯一性不证明平台副作用唯一；仅沿用可靠性探针的独立effects账本指标。"}


def analyze(catalog, root=ROOT):
    """只调用既有纯评分函数；不调用运行器的run_suite或业务查询/对账入口。"""
    from evals import reliability_runner as v1, reliability_v2_runner as v2
    from evals.recovery_analysis import analyze as recovery_analysis
    from evals.content_live_runner import Case, score_case
    from rpa_agent_module.models import verified_draft_result

    root = Path(root)
    tasks, jobs, latencies, results = [], [], [], {}
    definitions = {
        "cohort": "各source_id单独统计，不合并修复前后、v1/v2重评分、重放或原批/复测。",
        "automatic_completion": "同目标任务中，宿主核验完成且能证明无需异常人工救援；触发者不明为unknown，正常审批不计救援。",
        "human_rescue": "执行异常后需要额外人工解决；没有人工事件不能推断无人介入；等待核验与已记录人工结论分别计数。",
        "end_to_end": "宿主最终任务判定时间减任务受理时间；缺任一端点为null；未完成任务不补零。",
        "agent_active": "完整agent_run/agent_receipt配对的duration_ms之和；包含工具，不是纯模型时间或完整业务墙钟时间。",
        "rpa": "finished_at-started_at；排队为started_at-created_at；数据库原始时区保留。",
        "browser_readback": "同一Robot XML内verifying_draft开始至最后实际执行的任务关键字结束，排除NOT RUN与teardown；含轮询等待。",
        "host_verification": "宿主二次query_verified_draft/CompletionContract核验；没有独立跨度，保持null。",
        "percentile": "nearest-rank ceil(p*N)，按来源、阶段与终态分组，输出observed和missing；不可把嵌套跨度相加。",
        "save_success": "完整读回与原执行/Job/审核版本匹配；真实平台成功还要求platform=real，URL或success字段不足以认证。",
        "rpa_job_success": "succeeded唯一Job/全部已创建唯一Job；故障矩阵和正常样本分开，不作为生产成功概率。",
        "readback_pass": "完整读回通过/已进入读回核验；另列完整核验通过/全部Job，防止跳过核验抬高结果。",
        "duplicates": "请求重放、额外Job和额外平台副作用分别记录；外部副作用只接受独立保存账本。",
        "legacy": "旧库消息completed、Action draft_saved均为声明；无新版执行/Job/读回链时不能认证业务完成。",
    }

    for source in catalog.sources:
        source_tasks, source_jobs = [], []
        path = root / source.path
        if source.kind == "reliability":
            raw = read_json(path)
            cases = v1.load_dataset().cases
            # 现有评分器会改写passed，必须在副本上工作，不修改原始证据。
            scored_rows = deepcopy(raw["cases"])
            summary = v1.summarize(cases, scored_rows, raw["summary"]["repeated_runs"]["k"])
            recovery = recovery_analysis(raw)
            recovered = {(r["case_id"], r["repetition"]): r for r in recovery["case_runs"]}
            revised = v2.rescore(raw) if source.rescore_v2 else None
            contracts = {c.case_id: c for c in cases}
            for index, row in enumerate(scored_rows):
                contract = contracts[row["case_id"]]
                evidence = row.get("evidence", row.get("raw", {}))
                executions = evidence.get("executions", [])
                states = [e["status"] for e in executions]
                terminal = states[0] if len(states) == 1 else "no_external_execution" if not states else "multiple_executions"
                if row["status"] in {"not_run", "interrupted", "harness_error"}:
                    terminal = row["status"]
                task = task_row(source, f"{row['case_id']}:{row['repetition']}",
                    case_id=row["case_id"], repetition=row["repetition"], task_type="external_save_probe" if executions else "scenario_contract",
                    json_pointer=f"/cases/{index}", terminal=terminal, contract_passed=row["passed"],
                    failure_reasons=row["failure_reasons"], recovery_category=recovered.get((row["case_id"], row["repetition"]), {}).get("category"),
                    execution_ids=[e["execution_id"] for e in executions], job_ids=[j["id"] for j in evidence.get("jobs", [])],
                    human_resolution_recorded=any(s in {"manually_confirmed", "reviewed_not_saved"} for s in states)
                        or any(d.get("human_resolution") for d in evidence.get("drafts", [])),
                    needs_human_review="needs_review" in states,
                    normal_approval_observed=any(d.get("approval") or d.get("approval_version") for d in evidence.get("drafts", [])),
                    post_fault_trigger="test_harness_not_human_telemetry" if "recovery" in contract.tags else "not_applicable")
                verified = []
                for job in evidence.get("jobs", []):
                    full = "input" in job and "result" in job
                    check = verified_draft_result(job["result"], job["input"]) if full else job.get("verified")
                    matching = any(e["execution_id"] == job.get("execution_id") and e.get("job_id") == job["id"] for e in executions)
                    verified.append(check is True and matching and job["status"] == "succeeded")
                    entry = {"source_id": source.id, "task_id": task["task_id"], "source_path": source.path,
                        "json_pointer": f"/cases/{index}", "job_id": job["id"], "execution_id": job.get("execution_id"),
                        "job_status": job["status"], "platform": source.platform, "model": source.model,
                        "executor": source.executor, "data_origin": source.data_origin,
                        "canonical": True, "readback_verified": check,
                        "readback_attempted": True if check is True or (full and isinstance(job["result"], dict) and "verification" in job["result"]) else None,
                        "verification_evidence": "full_archived_readback" if full else "synthetic_scorer_projection",
                        "created_at": job.get("created_at"), "started_at": job.get("started_at"), "finished_at": job.get("finished_at")}
                    source_jobs.append(entry)
                    add_latency(latencies, task, "rpa_execution", elapsed_ms(entry["started_at"], entry["finished_at"]),
                                "报告仅保留Job投影，缺少起止时间", job_id=job["id"],
                                span_start=entry["started_at"], span_end=entry["finished_at"], clock="job_database")
                    add_latency(latencies, task, "rpa_queue", elapsed_ms(entry["created_at"], entry["started_at"]),
                                "报告缺少排队时间端点", job_id=job["id"],
                                span_start=entry["created_at"], span_end=entry["started_at"], clock="job_database")
                task["machine_verified"] = bool(verified) and all(verified) and "draft_saved" in states
                if terminal == "draft_saved" and not task["machine_verified"]:
                    task.update(terminal="unverified_success_claim", declared_status=terminal)
                source_tasks.append(task)
                add_latency(latencies, task, "harness_case", row.get("duration_ms"), "原报告未观测耗时")
                add_latency(latencies, task, "end_to_end", None, "缺少任务受理时间与宿主终态判定时间")
            results[source.id] = {"label": source.label, "kind": source.kind,
                "v1_summary": summary, "v1_summary_matches_original": summary == raw["summary"],
                "v2_summary": revised["summary"] if revised else None, "recovery": recovery,
                "scope": "固定SDK、合成来源与平台；v1/v2同一观察，非独立模型实验"}

        elif source.kind == "robot":
            index = read_json(path)
            folder = root / source.evidence_root
            canonical = {r["job_id"]: r for r in index["scenario_evidence"]}
            seen = set()
            for dbpath in sorted(folder.rglob("jobs.db")):
                case_folder = dbpath.parent.parent
                receipt_path = case_folder / "acceptance.json"
                receipt = read_json(receipt_path) if receipt_path.is_file() else None
                for job in query_rows(dbpath, "SELECT * FROM jobs ORDER BY id"):
                    if job["id"] in seen:
                        continue
                    seen.add(job["id"])
                    selected = canonical.get(job["id"])
                    if selected and (selected["execution_id"] != job["execution_id"]
                                     or selected["receipt_sha256"] != fingerprint(receipt_path)):
                        raise ValueError("Robot目录与原始索引关联不一致")
                    payload = json.loads(job["input_json"])
                    result = json.loads(job["result_json"]) if job["result_json"] else None
                    xmlpath = dbpath.parent / "jobs" / job["id"] / "output.xml"
                    if selected and selected["robot_output_sha256"] != fingerprint(xmlpath):
                        raise ValueError("Robot原始XML与索引不一致")
                    readback = xml_readback(xmlpath)
                    checked = verified_draft_result(result, payload)
                    business_db = case_folder / "web.sqlite3"
                    execution = query_rows(business_db, "SELECT * FROM content_draft_executions WHERE execution_id=?", (job["execution_id"],)) if business_db.is_file() else []
                    draft = query_rows(business_db, "SELECT data_json FROM content_draft_versions WHERE artifact_id=?", (execution[0]["artifact_id"],)) if execution else []
                    target = json.loads(draft[0]["data_json"]) if draft else {}
                    approval = target.get("approval") or {}
                    authorized = bool(target) and approval.get("version") == target.get("version") and approval.get("result_ref") == target.get("result_ref")
                    linked = bool(execution) and execution[0]["job_id"] == job["id"]
                    machine_verified = checked and authorized and linked and job["status"] == "succeeded" and verified_draft_result(result, target)
                    state = execution[0]["status"] if execution else "unknown_business_state"
                    task = task_row(source, job["execution_id"] or job["id"],
                        case_id=case_folder.name, repetition=None, task_type="external_save", terminal=state,
                        source_path=str(business_db.relative_to(root)) if business_db.is_file() else str(dbpath.relative_to(root)),
                        json_pointer="content_draft_executions.execution_id=" + str(job["execution_id"]),
                        execution_ids=[job["execution_id"]], job_ids=[job["id"]], canonical=bool(selected),
                        machine_verified=machine_verified and state == "draft_saved",
                        normal_approval_observed=authorized, human_resolution_recorded=bool(target.get("human_resolution")),
                        needs_human_review=state == "needs_review", post_fault_trigger="test_operator_or_refresh_not_observed_human_rescue")
                    source_tasks.append(task)
                    entry = {"source_id": source.id, "task_id": task["task_id"], "source_path": str(dbpath.relative_to(root)),
                        "job_id": job["id"], "execution_id": job["execution_id"], "job_status": job["status"],
                        "business_status": state, "platform": source.platform, "model": source.model,
                        "executor": source.executor, "data_origin": source.data_origin,
                        "canonical": bool(selected), "readback_verified": checked, "readback_attempted": readback["attempted"],
                        "verification_evidence": "full_archived_readback", "authorized_and_linked": authorized and linked,
                        "receipt_present": receipt is not None, "xml_path": str(xmlpath.relative_to(root)),
                        "created_at": job["created_at"], "started_at": job["started_at"], "finished_at": job["finished_at"]}
                    source_jobs.append(entry)
                    spans = {"rpa_execution": (job["started_at"], job["finished_at"], "job_database"),
                             "rpa_queue": (job["created_at"], job["started_at"], "job_database"),
                             "browser_readback": (readback["start"], readback["end"], "robot_xml_same_file")}
                    for stage, value, reason in [
                        ("rpa_execution", elapsed_ms(job["started_at"], job["finished_at"]), "Job缺少起止时间"),
                        ("rpa_queue", elapsed_ms(job["created_at"], job["started_at"]), "Job缺少排队时间端点"),
                        ("browser_readback", readback["duration_ms"], "执行未进入读回阶段或XML缺失"),
                        ("host_verification", None, "宿主二次核验没有独立计时"),
                        ("end_to_end", None, "业务执行表无受理/终态时间；Action.created_at是完成后写入")]:
                        start, end, clock = spans.get(stage, (None, None, "not_observed"))
                        add_latency(latencies, task, stage, value, reason, job_id=job["id"], canonical=bool(selected),
                                    span_start=start, span_end=end, clock=clock)
            if set(canonical) - seen:
                raise ValueError("原始11场景存在缺失Job，不能沿用完整分母")
            results[source.id] = {"label": source.label, "kind": source.kind,
                "canonical": summarize_jobs([r for r in source_jobs if r["canonical"]]),
                "all_existing_attempts": summarize_jobs(source_jobs),
                "canonical_business": summarize_tasks([r for r in source_tasks if r["canonical"]]),
                "earlier_failures_retained": index["earlier_failed_runs_retained"],
                "failure_reference_coverage": [{"path": name, "present": (folder / name).is_file()}
                                               for name in index["earlier_failed_runs_retained"]],
                "scope": "分母仅为现存已创建Job；11场景是跨批覆盖集，额外Job单列；缺失中断报告与未创建Job的尝试分母未知，不冒充完整历史或生产成功率"}

        elif source.kind == "live_content":
            report = read_json(path)
            cases = {r["id"]: Case.model_validate(r) for r in report["manifest"]["cases"]}
            expected = {(c, n) for c in cases for n in range(1, report["manifest"]["repetitions"] + 1)}
            if len(report["results"]) != len(expected) or {(r["case_id"], r["repetition"]) for r in report["results"]} != expected:
                raise ValueError("真实模型历史报告缺少计划运行或存在重复行")
            normal_completed = correct_refusal = 0
            checks_identical = True
            for n, row in enumerate(report["results"]):
                case = cases[row["case_id"]]
                events = row["events"]
                score = score_case(case, events, row["snapshot"], any(e["event"] == "sdk_request_blocked" for e in events)) if row.get("snapshot") else None
                status = score["mechanical_status"] if score and row["status"] not in {"interrupted", "harness_error"} else row["status"]
                checks_identical &= not score or score["checks"] == row["score"]["checks"]
                normal_completed += status == "normal_completed"
                correct_refusal += status == "correct_refusal"
                task = task_row(source, f"{row['case_id']}:{row['repetition']}",
                    case_id=row["case_id"], repetition=row["repetition"], task_type="content_flow_" + case.kind, terminal=status,
                    json_pointer=f"/results/{n}", machine_verified=None,
                    contract_passed=status in {"normal_completed", "correct_refusal"},
                    normal_approval_observed=any(e["event"] == "human_goal_confirmation" and e["status_code"] == 200 for e in events),
                    post_fault_trigger="test_operator_actions_not_human_telemetry",
                    failure_reasons=[k for k, ok in score["checks"].items() if not ok] if score else [row.get("not_run_reason", status)],
                    candidate_versions=len(row.get("snapshot", {}).get("drafts", [])))
                source_tasks.append(task)
                runs = [e for e in events if e["event"] == "agent_run"]
                receipts = [e for e in events if e["event"] == "agent_receipt"]
                active = sum(e["duration_ms"] for e in receipts) if runs and len(runs) == len(receipts) and all(e.get("duration_ms") is not None for e in receipts) else None
                for stage, value, reason in [
                    ("harness_case", row.get("duration_ms"), "计划未运行或缺少场景计时"),
                    ("agent_active", active, "没有完整Agent运行/回执配对"),
                    ("end_to_end", None, "场景计时含夹具，未保存任务受理/终态墙钟时间"),
                    ("host_verification", None, "历史宿主未独立记录核验计时")]:
                    add_latency(latencies, task, stage, value, reason)
            normal_n = sum(r["kind"] == "normal" for r in report["results"])
            refusal_n = len(report["results"]) - normal_n
            results[source.id] = {"label": source.label, "kind": source.kind,
                "normal_mechanical_completion_rate": fraction(normal_completed, normal_n),
                "correct_refusal_rate": fraction(correct_refusal, refusal_n),
                "scenario_contract_rate": fraction(normal_completed + correct_refusal, len(report["results"])),
                "rescored_checks_match_original": checks_identical,
                "original_status_counts": report["summary"]["statuses"],
                "scope": "真实模型、合成来源、平台禁止执行；候选/研究/计划机械验收，不代表人工内容质量或外部保存"}

        else:
            tables = {r["name"] for r in query_rows(path, "SELECT name FROM sqlite_master WHERE type='table'")}
            details = query_rows(path, "SELECT d.*,m.status FROM message_execution_details d JOIN messages m USING(message_id)")
            actions = query_rows(path, "SELECT * FROM generation_ui_actions WHERE action_id='save_xiaohongshu_draft'")
            for action in actions:
                response = json.loads(action["response_json"])
                source_tasks.append(task_row(source, action["idempotency_key"], task_type="legacy_save_action",
                    terminal="unverified_legacy", declared_status=response.get("status"),
                    json_pointer="generation_ui_actions.idempotency_key=" + action["idempotency_key"],
                    machine_verified=None, needs_human_review=response.get("status") == "needs_user_action"))
            for detail in details:
                task = task_row(source, f"message:{detail['message_id']}", terminal=detail["status"],
                                json_pointer=f"message_execution_details.message_id={detail['message_id']}")
                add_latency(latencies, task, "web_message_handler", detail["duration_ms"], "历史消息未记录耗时")
            results[source.id] = {"label": source.label, "kind": source.kind,
                "tables_present": sorted(tables), "new_execution_table_present": "content_draft_executions" in tables,
                "message_details": len(details), "message_duration_observed": sum(d["duration_ms"] is not None for d in details),
                "save_action_status_claims": dict(Counter(r["declared_status"] for r in source_tasks)),
                "scope": "旧库声明不可认证完成；保存Action不是全部任务分母，消息状态不是业务终态"}

        results[source.id]["task_observations"] = summarize_tasks(source_tasks)
        if source_jobs:
            results[source.id]["job_observations"] = summarize_jobs(source_jobs)
        tasks.extend(source_tasks)
        jobs.extend(source_jobs)

    grouped = defaultdict(list)
    for row in latencies:
        grouped[(row["source_id"], row["stage"], row["terminal"], row.get("canonical"), row.get("contract_passed"))].append(row["duration_ms"])
    timing = [{"source_id": k[0], "stage": k[1], "terminal": k[2], "canonical": k[3], "contract_passed": k[4], **latency_summary(v)} for k, v in sorted(grouped.items(), key=lambda x: str(x[0]))]
    real = [r for r in tasks if r["platform"] == "real" and r["task_type"] == "external_save"]
    resume = [
        "构建50个固定离线场景，每场景重复3次，新版执行契约150/150通过；覆盖正确拒绝与故障处置，不等于真实模型任务成功率。",
        "通过宿主完成核验，将固定合成故障集的假完成探针由3/9降至0/9。",
        "基于独立副作用账本，在30次定向合成探针中未观察到重复保存。",
        "在36次禁止执行合成探针中未观察到未审核RPA提交。",
        "45次合成恢复场景均满足故障处置契约，其中21次以核验保存结束、12次进入或记录人工处置、12次安全停止；不声称无人干预恢复率。",
    ]
    metrics = {"schema_version": 1, "definitions": definitions, "sources": results, "latency_groups": timing,
        "real_platform": {"observed_save_intents": len(real), "certified_save_samples": sum(r["machine_verified"] is True for r in real),
                          "verified_save_rate": fraction(sum(r["machine_verified"] is True for r in real), len(real)),
                          "automatic_completion_rate": fraction(0, len(real), len(real)),
                          "note": "本清单没有可认证真实平台保存样本；空分母为null，不是0%或100%。"},
        "missing_data": [
            {"metric": "end_to_end", "fields": ["task_accepted_at", "task_terminal_at", "logical_task_id_to_execution_id"], "minimal_instrumentation": "仅建议：业务入口与最终宿主结论各记录一条已有日志格式事件，并关联原execution_id/job_id。"},
            {"metric": "host_verification", "fields": ["verification_started_at", "verification_finished_at"], "minimal_instrumentation": "仅建议：现有核验调用前后记录时间；浏览器XML读回耗时不能替代宿主二次核验。"},
            {"metric": "automatic_completion_and_human_rescue", "fields": ["post_fault_trigger_actor", "human_action_kind", "observation_complete"], "minimal_instrumentation": "仅建议：区分正常审批、异常救援、人工核验与系统对账；记录触发者和关联任务，不从无记录推断无人介入。"},
            {"metric": "real_platform_save_and_duplicates", "fields": ["trusted_platform_provenance", "full_readback", "external_operation_ledger"], "minimal_instrumentation": "仅建议：真实平台运行另行授权，保留完整读回与平台侧操作证据；Job数量不代替外部副作用数。"}],
        "resume_statements": resume,
        "limitations": ["只分析已有证据，不运行场景或模型，不修改生产逻辑、埋点、数据库。", "各来源和终态分开，固定故障矩阵不是线上发生概率。", "人工事件多为合成测试操作者动作；正常审批不计异常救援，未知不补零。", "源码以快照/哈希为准，HEAD不代表完整dirty被测版本。"]}
    return {"metrics": metrics, "tasks": tasks, "jobs": jobs, "latencies": latencies}


def render_report(bundle):
    metrics = bundle["metrics"]
    lines = ["# TestAgent 业务执行量化评测", "", "本报告只读分析已有证据；未发起模型、浏览器或真实平台调用。", "",
             "## 既有指标复算", "", "| 批次 | v1契约 | v2契约 | 假完成 | 恢复处置 | 重复副作用 | 未授权 |", "|---|---:|---:|---:|---:|---:|---:|"]
    for sid, result in metrics["sources"].items():
        if result["kind"] != "reliability":
            continue
        old, new = result["v1_summary"], result["v2_summary"]
        m = old["metrics"]
        rates = [f"{m[k]['numerator']}/{m[k]['denominator']}" for k in ("false_completion_rate", "recovery_success_rate", "duplicate_side_effect_rate", "unauthorized_action_rate")]
        lines.append(f"| {sid} | {old['passed']}/{old['planned']} | {str(new['passed']) + '/' + str(new['planned']) if new else '不重评'} | " + " | ".join(rates) + " |")
    lines += ["", "v1/v2是同次观察的两套契约，不相加；各阶段、重放与真实模型原批/复测也不合并。旧契约失败与全部历史失败保留。", "",
              "## 恢复定义", "", "修复后45/45表示故障处置契约通过。21/45是契约通过且最终draft_saved，部分由测试显式对账或再次保存触发，不证明无人干预恢复。",
              "12/45转人工包括：manually_confirmed 3、reviewed_not_saved 3、needs_review 6；另12/45为安全停止。人工确认存在不等于机器完整核验。", "",
              "修复后另有45条持久外部保存意图，27条以完整核验保存结束。它与恢复场景的45次不是同一集合，不能将27/45或21/45写成生产自动完成率。", "",
              "## 来源、终态与自动完成边界", "", "| 来源 | 任务观察 | 终态分布 | 机械核验保存 | 正常审批观察 | 异常救援率 |", "|---|---:|---|---:|---:|---|"]
    for sid, result in metrics["sources"].items():
        observed = result["task_observations"]
        lines.append(f"| {sid} | {observed['tasks']} | {json.dumps(observed['terminal_counts'], ensure_ascii=False)} | {observed['machine_verified_results']} | {observed['normal_approval_observed']} | null：触发者/完整人工日志不足 |")
    robot = metrics["sources"]["robot_loopback"]
    live0, live1 = metrics["sources"]["live_original"], metrics["sources"]["live_retest"]
    lines += ["", "### Robot模拟平台结果", "", "| 集合 | 已创建Job | Job成功 | 完整读回/全部Job | 完整读回/进入核验 | 额外Job/执行 |", "|---|---:|---:|---:|---:|---:|"]
    for label, cohort in [("跨批11场景", robot["canonical"]), ("全部现存Job", robot["all_existing_attempts"])]:
        rate = cohort["readback_pass_rate_among_attempted"]
        lines.append(f"| {label} | {cohort['jobs']} | {cohort['status_counts'].get('succeeded', 0)} | {cohort['verified_readback_per_created_job']['numerator']}/{cohort['jobs']} | {rate['numerator']}/{rate['denominator']} | {cohort['extra_jobs_per_execution']} |")
    missing_refs = [r["path"] for r in robot["failure_reference_coverage"] if not r["present"]]
    lines += ["", "分母仅为现存已创建Job，含故障注入；额外尝试单列，不能称为单批全绿或线上成功概率。0个额外Job不等于0次重复平台副作用，后者没有独立账本，保持null。", "",
              f"原摘要引用的先前失败/中断证据缺失项：{json.dumps(missing_refs, ensure_ascii=False)}。索引保留；不能把缺失项补为通过，也不能声称完整恢复全部历史尝试分母。", "",
              f"真实模型原批：{json.dumps(live0['original_status_counts'], ensure_ascii=False)}；复测：{json.dumps(live1['original_status_counts'], ensure_ascii=False)}。", "",
              "正常任务机械完成与正确拒绝分别计算；真实模型只到候选/研究/计划，平台为禁止执行替身，人工内容质量仍待评。", "",
              "真实外部平台保存成功率=null：没有已认证样本。旧库6次draft_saved声明缺少执行/Job/完整读回链，不认证为真实业务保存。", "",
              "## 耗时分布", "", "单位ms。harness_case含夹具与故障等待；agent_active为各Agent.run跨度之和；RPA包含浏览器与读回。嵌套阶段不能相加。", "",
              "| 来源 | 阶段 | 终态 | canonical | 原契约通过 | 有效 | 缺失 | P50 | P95 |", "|---|---|---|---|---|---:|---:|---:|---:|"]
    for row in metrics["latency_groups"]:
        numbers = ["null" if row[k] is None else f"{row[k]:.3f}" for k in ("p50_ms", "p95_ms")]
        lines.append(f"| {row['source_id']} | {row['stage']} | {row['terminal']} | {row['canonical']} | {row['contract_passed']} | {row['observed']} | {row['missing']} | {numbers[0]} | {numbers[1]} |")
    lines += ["", "## 统计口径", ""]
    lines.extend(f"- **{name}**：{definition}" for name, definition in metrics["definitions"].items())
    lines += ["", "## 不可测指标与最小埋点建议（未实施）", ""]
    for item in metrics["missing_data"]:
        lines.append(f"- {item['metric']}：缺少{', '.join(item['fields'])}。{item['minimal_instrumentation']}")
    lines += ["", "## 简历表述", ""] + [f"{n}. {s}" for n, s in enumerate(metrics["resume_statements"], 1)]
    lines += ["", "## 追溯与复现", "", "manifest.json列出每个原始输入及SHA256、源码指纹、Git HEAD/dirty状态。tasks.csv保留source_id、原始行/JSON指针、执行与Job关联；jobs.csv和latencies.csv保留逐次观测。", "",
              "使用项目现有依赖，从项目根目录执行（输出目录必须不存在）：", "", "```sh", "PYTHONDONTWRITEBYTECODE=1 PYTHON_DOTENV_DISABLED=1 .venv/bin/python -B -m evals.business_execution_metrics --output-dir artifacts/evals/business-execution/NEW", "```", "",
              "输入哈希变化即停止，需要另外冻结来源版本；不改旧真值或失败来获得新数字。", ""]
    return "\n".join(lines)


def write_outputs(bundle, catalog, sources_path, output, root=ROOT):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    for name in ("tasks", "jobs", "latencies"):
        rows = bundle[name]
        columns = list(dict.fromkeys(k for row in rows for k in row))
        with (output / f"{name}.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            for row in rows:
                writer.writerow({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v for k, v in row.items()})
    (output / "metrics.json").write_text(json.dumps(bundle["metrics"], ensure_ascii=False, indent=2) + "\n", "utf-8")
    manifest = {"schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
        "sources_path": str(Path(sources_path).resolve().relative_to(Path(root).resolve())) if Path(sources_path).resolve().is_relative_to(Path(root).resolve()) else str(Path(sources_path).resolve()),
        "sources_sha256": fingerprint(sources_path), "inputs_sha256": catalog.files_sha256,
        "analysis_sha256": fingerprint(__file__), "python": os.sys.version, "sqlite": sqlite3.sqlite_version,
        "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"}, text=True).strip(),
        "git_status": subprocess.check_output(["git", "status", "--porcelain=v1", "-uall"], cwd=root, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"}, text=True),
        "side_effects": {"scenario_runs": 0, "model_calls": 0, "platform_calls": 0, "database_writes": 0},
        "output_sha256": {p.name: fingerprint(p) for p in sorted(output.iterdir()) if p.is_file()}}
    (output / "report.md").write_text(render_report(bundle), "utf-8")
    manifest["output_sha256"]["report.md"] = fingerprint(output / "report.md")
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", "utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", type=Path, default=DEFAULT_SOURCES)
    parser.add_argument("--output-dir", type=Path, required=True)
    options = parser.parse_args()
    catalog = load_catalog(options.sources)
    if options.output_dir.exists():
        raise FileExistsError(options.output_dir)
    bundle = analyze(catalog)
    # 再核对一次输入，避免把分析期间已变化的证据报告为同一冻结批次。
    load_catalog(options.sources)
    write_outputs(bundle, catalog, options.sources, options.output_dir)
    print(json.dumps({"output": str(options.output_dir.resolve()), "sources": len(catalog.sources),
                      "task_rows": len(bundle["tasks"]), "job_rows": len(bundle["jobs"]), "latency_rows": len(bundle["latencies"])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
