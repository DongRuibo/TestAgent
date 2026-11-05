"""14.8 投流计划：冻结已有数据、校验建议并保存待审批版本，不执行投放。"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
import re
import tempfile
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from testagent_dev_agent.paths import REPOSITORY_ROOT
from tools.content_research import load_content_research_artifact, safe_research_draft
from tools.operation_goals import OperationGoalStore
from tools.rpa_xiaohongshu_metrics import collect_xiaohongshu_metrics


PROMOTION_DIR = REPOSITORY_ROOT / "workspace" / "promotion_plans"
METRICS_DIR = REPOSITORY_ROOT / "workspace" / "xiaohongshu_metrics"
MAX_BYTES = 2_000_000
Text = Annotated[str, Field(min_length=1, max_length=1500)]
ShortText = Annotated[str, Field(min_length=1, max_length=300)]
Money = Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=2, allow_inf_nan=False)]
Count = Annotated[int, Field(ge=0, strict=True)]
Metric = Literal["impressions", "views", "cover_click_rate", "avg_view_seconds", "likes",
                 "comments", "saves", "shares", "followers_gained", "danmaku", "spend", "inquiries", "leads", "sales"]
LABELS = {"impressions": "曝光", "views": "观看", "cover_click_rate": "封面点击率",
          "avg_view_seconds": "人均观看秒数", "likes": "点赞", "comments": "评论", "saves": "收藏",
          "shares": "分享", "followers_gained": "涨粉", "danmaku": "弹幕", "spend": "花费",
          "inquiries": "咨询", "leads": "有效线索", "sales": "成交"}
RECOMMENDATIONS = {"prioritize": "优先投流候选", "test": "小额试投候选",
                   "optimize_first": "先优化再考虑投流", "hold": "暂不投流"}
NO_SPEND = {"optimize_first", "hold"}
MISSING_LABELS = {"company_name": "企业名称", "company_profile": "企业资料", "objective": "本次投流目标",
                  "primary_metric": "主要衡量指标", "target_audience": "目标人群", "regions": "投流地区",
                  "start_date": "开始日期", "end_date": "结束日期", "budget_cap_cny": "预算上限",
                  "budget_basis": "预算依据", "market_research": "市场研究", "posts": "后台笔记数据",
                  "post_url": "笔记链接", "budget_cny": "本篇分配预算", "research_evidence": "研究依据"}
LIMITATIONS = [
    "后台日期按笔记首发时间筛选，不代表该时段新增流量；统计周期与自然/付费流量划分未明确。",
    "自然表现和公开热门笔记不能证明投放效果；本版预期结果均为待验证的测试目标。",
    "不计算获客成本或 ROI，不评估投后表现；止损和追加条件仅供人工审批及执行时参考。",
    "预算是调用方提供的约束，不是支出授权。任何产生费用的动作及追加预算必须由有权人类事前审批。",
    "笔记链接由调用方提供，审批时需核对账号和笔记；系统未自动验证其与后台记录的关联。",
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class PromotionInputs(StrictModel):
    metrics_artifact_path: Annotated[str, Field(min_length=1, max_length=2000)] | None = None
    goal_artifact_path: str | None = Field(default=None, max_length=2000)
    research_artifact_path: str | None = Field(default=None, max_length=2000)
    company_name: ShortText | None = None
    company_profile: Text | None = None
    objective: Text | None = None
    primary_metric: Metric | None = None
    target_audience: list[ShortText] = Field(default_factory=list, max_length=10)
    regions: list[ShortText] = Field(default_factory=list, max_length=10)
    start_date: date | None = None
    end_date: date | None = None
    budget_cap_cny: Money | None = None
    budget_basis: Text | None = None

    @model_validator(mode="after")
    def ordered_dates(self):
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValueError("投流结束日期不能早于开始日期。")
        return self


class MetricsPost(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True, allow_inf_nan=False)
    title: ShortText
    published_at: datetime
    content_type: ShortText
    content_id: ShortText | None = None
    impressions: Count | None = None
    views: Count | None = None
    likes: Count | None = None
    comments: Count | None = None
    saves: Count | None = None
    shares: Count | None = None
    followers_gained: Count | None = None
    danmaku: Count | None = None
    cover_click_rate: Annotated[float, Field(ge=0, le=1, strict=True)] | None = None
    avg_view_seconds: Annotated[float, Field(ge=0, strict=True)] | None = None
    spend: None = None
    inquiries: None = None
    leads: None = None

    @field_validator("published_at")
    @classmethod
    def aware_time(cls, value):
        if value.tzinfo is None:
            raise ValueError("笔记发布时间必须包含时区。")
        return value


class TestTarget(StrictModel):
    description: Text
    basis: Text


class PlanCondition(StrictModel):
    condition: Text
    required_data: list[Metric] = Field(min_length=1, max_length=10)


class PromotionCandidate(StrictModel):
    post_index: int = Field(ge=0, strict=True)
    recommendation: Literal["prioritize", "test", "optimize_first", "hold"] = "test"
    post_url: str | None = Field(default=None, max_length=2000)
    reason: Text
    hypothesis: Text
    research_source_refs: list[ShortText] = Field(default_factory=list, max_length=10)
    budget_cny: Money | None = None
    expected_result: TestTarget
    stop_conditions: list[PlanCondition] = Field(default_factory=list, max_length=5)
    additional_budget_conditions: list[PlanCondition] = Field(default_factory=list, max_length=5)

    @model_validator(mode="after")
    def spending_matches_recommendation(self):
        if self.recommendation in NO_SPEND:
            if self.budget_cny is not None:
                raise ValueError("暂不投流或先优化的笔记不得分配本轮投流预算。")
        elif not self.stop_conditions or not self.additional_budget_conditions:
            raise ValueError("建议投流的笔记必须说明止损和追加预算条件。")
        return self

    @field_validator("post_url")
    @classmethod
    def note_url(cls, value):
        if value is None:
            return value
        # 只存无查询参数的公开笔记定位，避免把分享 Token 写入计划。
        if not re.fullmatch(r"https://www\.xiaohongshu\.com/(?:explore|discovery/item)/[0-9a-fA-F]{24}", value):
            raise ValueError("笔记链接须为 https://www.xiaohongshu.com/explore/<24位ID>，不带查询参数。")
        return value


class PromotionSave(StrictModel):
    input_artifact_path: Annotated[str, Field(min_length=1, max_length=2000)]
    summary: Text
    candidates: list[PromotionCandidate] = Field(min_length=1, max_length=10)
    assumptions: list[Text] = Field(min_length=1, max_length=20)
    planning_suggestions: Text | None = None
    previous_plan_artifact_path: str | None = Field(default=None, max_length=2000)
    revision_reason: Text | None = None


class PromotionRead(StrictModel):
    artifact_path: Annotated[str, Field(min_length=1, max_length=2000)]
    section: Literal["overview", "goal", "posts", "research", "candidates"] = "overview"
    offset: int = Field(default=0, ge=0, strict=True)
    limit: int = Field(default=5, ge=1, le=5, strict=True)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _resolve(path: str | Path, root: Path, filename: str | None = None) -> Path:
    raw = Path(path).expanduser().absolute()
    resolved = raw.resolve()
    if (not resolved.is_relative_to(root.resolve()) or (filename and resolved.name != filename)
            or any(part.is_symlink() for part in (raw, *raw.parents))):
        raise ValueError("文件必须位于指定工件目录，且路径不能包含符号链接。")
    return resolved


def _read(path):
    if path.stat().st_size > MAX_BYTES:
        raise ValueError("工件超过 2 MB。")
    value = json.loads(path.read_text("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("工件必须是 JSON 对象。")
    return value


def _write(path, content):
    if len(content.encode()) > MAX_BYTES:
        raise ValueError("工件超过 2 MB。")
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent) as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())
        os.link(output.name, path)


def _load_metrics(path):
    path = _resolve(path, METRICS_DIR, "metrics.json")
    raw = _read(path)
    if (type(raw.get("schema_version")) is not int or raw["schema_version"] != 1
            or raw.get("source", {}).get("kind") != "creator_official_export"
            or raw.get("source", {}).get("platform") != "xiaohongshu"
            or raw.get("date_filter_kind") != "first_published_at"
            or raw.get("metrics_period") != "unspecified_by_export"
            or raw.get("traffic_scope") != "unspecified_by_export"
            or not raw.get("account", {}).get("red_id")):
        raise ValueError("需要现有采集工具输出的官方后台 metrics.json。")
    posts = raw.get("posts")
    if (not isinstance(posts, list) or len(posts) > 50
            or type(raw.get("collected_count")) is not int or raw["collected_count"] != len(posts)
            or type(raw.get("source_total")) is not int or raw["source_total"] < len(posts)):
        raise ValueError("后台记录条数不一致或超出范围。")
    normalized = [MetricsPost.model_validate(post).model_dump(mode="json") for post in posts]
    keys = [(post["title"], post["published_at"], post["content_type"]) for post in normalized]
    if len(keys) != len(set(keys)):
        raise ValueError("后台笔记记录重复，无法唯一引用。")
    # 从逐篇原始字段取基线，不使用模型输入或文件中的汇总代替。
    return {**raw, "posts": normalized}, {"path": str(path), "sha256": _hash(raw)}


def _load_saved(path, filename):
    path = _resolve(path, PROMOTION_DIR, filename)
    data = _read(path)
    if data.get("content_hash") != _hash({key: value for key, value in data.items() if key != "content_hash"}):
        raise ValueError("工件校验失败，内容可能已被修改。")
    if filename == "input.json":
        expected = PROMOTION_DIR / "inputs" / str(UUID(data["input_id"])) / filename
    else:
        expected = PROMOTION_DIR / str(UUID(data["plan_id"])) / f"v{data['version']}" / filename
        if data.get("approval_status") != "pending_approval":
            raise ValueError("投流计划只能保存待审批状态。")
    if path != expected.resolve():
        raise ValueError("工件身份与路径不一致。")
    return data


def _error(exc):
    if isinstance(exc, ValidationError):
        first = exc.errors(include_input=False, include_url=False, include_context=False)[0]
        message = "字段 " + ".".join(map(str, first["loc"])) + " 无效：" + first["msg"]
    elif isinstance(exc, OSError):
        message = "本地文件不存在、不可读写或版本已存在；请检查工件路径及最新版本。"
    elif isinstance(exc, (KeyError, TypeError, AttributeError)):
        message = "工件结构无效，请使用原采集或保存工具生成的文件。"
    else:
        message = str(exc)
    return {"success": False, "error_type": "INVALID_PROMOTION_PLAN",
            **safe_research_draft({"message": message}), "retryable": False}


ERRORS = (OSError, ValueError, TypeError, KeyError, AttributeError)


def prepare_promotion_plan(**payload: Any) -> dict[str, Any]:
    """未指定文件时采集一次绑定账号；冻结数据与约束，不调用模型或投流接口。"""
    try:
        request = PromotionInputs.model_validate(payload)
        business = request.model_dump(mode="json", exclude={"metrics_artifact_path", "goal_artifact_path", "research_artifact_path"})
        goal = (OperationGoalStore().load(request.goal_artifact_path, require_confirmed=True, require_latest=True)
                if request.goal_artifact_path else None)
        research = load_content_research_artifact(request.research_artifact_path) if request.research_artifact_path else None
        if goal:
            snapshot = goal["goal_snapshot"]
            for field in ("company_name", "company_profile"):
                if business[field] is not None and business[field] != snapshot[field]:
                    raise ValueError("企业资料与指定的运营目标不一致。")
                business[field] = snapshot[field]
            business["objective"] = business["objective"] or snapshot["operation_goal"]["summary"]
            business["target_audience"] = business["target_audience"] or snapshot["operation_goal"]["target_users"]
        if research:
            if goal and research.get("goal_ref") and research["goal_ref"] != goal["goal_ref"]:
                raise ValueError("研究引用的运营目标版本与本次目标不一致。")
            for field in ("company_name", "company_profile"):
                if business[field] is not None and business[field] != research[field]:
                    raise ValueError("研究企业资料与本次计划不一致。")
                business[field] = research[field]
            business["objective"] = business["objective"] or research["operation_goal"]
        metrics_path = request.metrics_artifact_path
        collection = {"mode": "existing"}
        if metrics_path is None:
            collected = collect_xiaohongshu_metrics()
            if not collected.get("success"):
                return {**safe_research_draft(collected), "stage": "collect_metrics"}
            metrics_path = REPOSITORY_ROOT / collected["path"]
            collection = {"mode": "fresh", "job_id": collected["job_id"]}
        metrics, metrics_ref = _load_metrics(metrics_path)
        missing = [field for field, value in business.items() if not value]
        if not research or research["execution_status"] == "failed":
            missing.append("market_research")
        if not metrics["posts"]:
            missing.append("posts")
        inputs = safe_research_draft({
            "artifact_type": "promotion_inputs", "schema_version": 1, "input_id": str(uuid4()),
            "created_at": datetime.now(timezone.utc).isoformat(), "business": business,
            "metrics": metrics, "goal": goal, "research": research, "collection": collection,
            "source_refs": {"metrics": metrics_ref,
                "goal": goal["goal_ref"] if goal else None,
                "research": {"path": str(Path(request.research_artifact_path).resolve()), "sha256": _hash(research)} if research else None},
            "missing_information": missing, "limitations": LIMITATIONS,
        })
        inputs["content_hash"] = _hash(inputs)
        path = _resolve(PROMOTION_DIR / "inputs" / inputs["input_id"] / "input.json", PROMOTION_DIR)
        path.parent.mkdir(parents=True, exist_ok=False)
        _write(path, _json(inputs))
        return {"success": True, "input_artifact_path": str(path), **_overview(inputs),
                "message": "输入已冻结。用 load_promotion_plan 分页读取 posts 和 research，分析后调用 save_promotion_plan。"}
    except ERRORS as exc:
        return _error(exc)


def _overview(inputs):
    return {"business": inputs["business"], "account": inputs["metrics"]["account"],
            "post_count": len(inputs["metrics"]["posts"]), "missing_information": inputs["missing_information"],
            "collection": inputs.get("collection"),
            "metrics_context": {key: inputs["metrics"].get(key) for key in (
                "collected_at", "query", "date_filter_kind", "metrics_period", "traffic_scope", "source_total", "exported_count", "truncated")},
            "limitations": inputs["limitations"], "source_refs": inputs["source_refs"]}


def load_promotion_plan(**payload: Any) -> dict[str, Any]:
    try:
        request = PromotionRead.model_validate(payload)
        filename = Path(request.artifact_path).name
        if filename not in {"input.json", "plan.json"}:
            raise ValueError("请选择 input.json 或 plan.json。")
        data = _load_saved(request.artifact_path, filename)
        inputs = data if filename == "input.json" else data["input_snapshot"]
        if request.section == "overview":
            return {"success": True, "artifact_path": request.artifact_path, **_overview(inputs),
                    "input_artifact_path": str(PROMOTION_DIR / "inputs" / inputs["input_id"] / "input.json"),
                    **({key: data.get(key) for key in ("plan_id", "version", "summary", "readiness", "approval_status", "missing_information", "budget", "planning_suggestions")} if filename == "plan.json" else {})}
        if request.section == "goal":
            goal = inputs["goal"]
            records = ([{"field": key, "value": value} for key, value in goal["goal_snapshot"]["operation_goal"].items()]
                       if goal else [])
        elif request.section == "posts":
            records = [{"post_index": i, **post} for i, post in enumerate(inputs["metrics"]["posts"])]
        elif request.section == "research":
            research = inputs["research"]
            if not research:
                records = []
            else:
                records = [{"kind": "summary", "text": research["research_summary"], "limitations": research["limitations"]}]
                for field in ("key_findings", "user_needs", "competitor_findings", "platform_findings"):
                    records += [{"group": field, **item} for item in research[field]]
                for topic in research["candidate_topics"]:
                    records += [{"group": "candidate_topics", "title": topic["title"], **evidence} for evidence in topic["evidence"]]
        else:
            if filename != "plan.json":
                raise ValueError("输入快照尚无投流候选方案。")
            records = data["candidates"]
        # 超长单项拆成带序号的 JSON 文本片段，仍可按 next_offset 完整读取。
        entries = []
        for index, record in enumerate(records):
            encoded = _json(record)
            if len(encoded) > 5500:
                fragments = [encoded[start:start + 5000] for start in range(0, len(encoded), 5000)]
                entries.extend({"format": "json_fragment", "record_index": index, "part": part + 1,
                                "parts": len(fragments), "text": fragment} for part, fragment in enumerate(fragments))
            else:
                entries.append(record)
        items = []
        for record in entries[request.offset:request.offset + request.limit]:
            # 预留工具结果包装开销，不让默认 8000 字符上下文截断整页证据。
            if items and len(_json([*items, record])) > 6000:
                break
            items.append(record)
        stop = request.offset + len(items)
        return {"success": True, "artifact_path": request.artifact_path, "section": request.section,
                "items": items, "total": len(entries), "source_record_count": len(records),
                "next_offset": stop if stop < len(entries) else None}
    except ERRORS as exc:
        return _error(exc)


def save_promotion_plan(**payload: Any) -> dict[str, Any]:
    try:
        request = PromotionSave.model_validate(payload)
        inputs = _load_saved(request.input_artifact_path, "input.json")
        posts = inputs["metrics"]["posts"]
        research = inputs["research"]
        source_ids = {source["source_id"] for source in research["source_references"]} if research else set()
        cap = inputs["business"]["budget_cap_cny"]
        allocated = sum((candidate.budget_cny or Decimal(0) for candidate in request.candidates), Decimal(0))
        if cap is None and allocated > 0:
            raise ValueError("预算上限尚未提供；金额建议请写入 planning_suggestions，候选分配暂留空。")
        if cap is not None and allocated > Decimal(cap):
            raise ValueError("候选笔记预算合计超过本次预算上限。")
        indices = [candidate.post_index for candidate in request.candidates]
        if len(indices) != len(set(indices)) or any(index >= len(posts) for index in indices):
            raise ValueError("候选笔记索引重复或不在输入数据中。")
        missing = list(inputs["missing_information"])
        candidates = []
        for candidate in request.candidates:
            if set(candidate.research_source_refs) - source_ids:
                raise ValueError("候选理由引用了不存在的研究来源。")
            if research and research["execution_status"] != "failed" and not candidate.research_source_refs:
                missing.append(f"posts[{candidate.post_index}].research_evidence")
            post = posts[candidate.post_index]
            if candidate.post_url and post["content_id"] and candidate.post_url.rsplit("/", 1)[-1] != post["content_id"]:
                raise ValueError("候选链接与后台笔记 ID 不一致。")
            for field in ("post_url", "budget_cny"):
                if field == "budget_cny" and candidate.recommendation in NO_SPEND:
                    continue
                if getattr(candidate, field) is None:
                    missing.append(f"posts[{candidate.post_index}].{field}")
            item = candidate.model_dump(mode="json")
            item["baseline"] = post
            item["expected_result"]["kind"] = "test_target_not_forecast"
            for field in ("stop_conditions", "additional_budget_conditions"):
                for condition in item[field]:
                    condition["missing_data"] = [metric for metric in condition["required_data"] if post.get(metric) is None]
                    condition["evaluation_status"] = "not_evaluated"
            candidates.append(item)
        plan_id, version, previous = str(uuid4()), 1, None
        if request.previous_plan_artifact_path:
            previous = _load_saved(request.previous_plan_artifact_path, "plan.json")
            if not request.revision_reason:
                raise ValueError("修订方案必须填写 revision_reason。")
            old = previous["input_snapshot"]
            if (old["business"]["company_name"] != inputs["business"]["company_name"]
                    or old["metrics"]["account"]["red_id"] != inputs["metrics"]["account"]["red_id"]):
                raise ValueError("不能把另一企业或账号的计划保存为本计划的新版本。")
            plan_id, version = previous["plan_id"], previous["version"] + 1
        complete_budget = all(candidate.budget_cny is not None or candidate.recommendation in NO_SPEND
                              for candidate in request.candidates)
        plan = safe_research_draft({
            "artifact_type": "promotion_plan", "schema_version": 1,
            "plan_id": plan_id, "version": version, "created_at": datetime.now(timezone.utc).isoformat(),
            "approval_status": "pending_approval", "readiness": "needs_information" if missing else "ready_for_review",
            "summary": request.summary, "assumptions": request.assumptions, "missing_information": missing,
            "planning_suggestions": request.planning_suggestions,
            "input_snapshot": inputs, "candidates": candidates, "budget": {
                "currency": "CNY", "cap": cap, "specified_allocation": str(allocated),
                "total": str(allocated) if complete_budget else None,
                "unallocated": str(Decimal(cap) - allocated) if cap is not None and complete_budget else None,
                "approval_status": "pending_approval"},
            "previous_plan": {"path": request.previous_plan_artifact_path, "content_hash": previous["content_hash"]} if previous else None,
            "revision_reason": request.revision_reason,
        })
        plan["content_hash"] = _hash(plan)
        content, report = _json(plan), _render_report(plan)
        if max(len(content.encode()), len(report.encode())) > MAX_BYTES:
            raise ValueError("方案超过 2 MB，请减少候选或输入内容。")
        path = _resolve(PROMOTION_DIR / plan_id / f"v{version}" / "plan.json", PROMOTION_DIR)
        path.parent.mkdir(parents=True, exist_ok=False)
        _write(path.with_name("report.md"), report)
        _write(path, content)
        return {"success": True, "plan_id": plan_id, "version": version,
                "approval_status": "pending_approval", "readiness": plan["readiness"],
                "artifact_path": str(path), "report_path": str(path.with_name("report.md")),
                "missing_information": missing, "budget": plan["budget"],
                "message": "投流计划已保存，等待人工补充或审批；尚未授权或执行投放。"}
    except ERRORS as exc:
        return _error(exc)


def _render_report(plan):
    inputs, budget = plan["input_snapshot"], plan["budget"]
    business = inputs["business"]

    def show(value):
        if value is None or value == []:
            return "待补充"
        return str(value).replace("<", "&lt;").replace(">", "&gt;")

    def missing_label(value):
        match = re.fullmatch(r"posts\[(\d+)\]\.(\w+)", value)
        if match:
            return f"候选 {int(match[1]) + 1}：{MISSING_LABELS.get(match[2], match[2])}"
        return MISSING_LABELS.get(value, value)

    lines = [f"# 投流计划 · 第 {plan['version']} 版", "", "状态：待人工审批；本报告不构成投放授权。", "",
        show(plan["summary"]), "", f"- 企业：{show(business['company_name'])}",
        f"- 账号：{show(inputs['metrics']['account'].get('nickname'))}（{show(inputs['metrics']['account']['red_id'])}）",
        f"- 目标：{show(business['objective'])}；主要指标：{show(LABELS.get(business['primary_metric']))}",
        f"- 目标人群：{show('、'.join(business['target_audience']) or None)}",
        f"- 地区：{show('、'.join(business['regions']) or None)}",
        f"- 周期：{show(business['start_date'])} 至 {show(business['end_date'])}",
        f"- 预算上限：{show(budget['cap'])} 元；本轮合计：{show(budget['total'])} 元；未分配：{show(budget['unallocated'])} 元",
        f"- 预算依据（调用方提供）：{show(business['budget_basis'])}", ""]
    if plan.get("planning_suggestions"):
        lines += ["## 建议设置（待确认，不替代上方输入约束）", "", show(plan["planning_suggestions"]), ""]
    for candidate in plan["candidates"]:
        baseline = candidate["baseline"]
        recommendation = candidate.get("recommendation", "test")
        allocation = ("本轮不分配" if recommendation in NO_SPEND else show(candidate["budget_cny"]) + " 元")
        lines += [f"## 候选 {candidate['post_index'] + 1}：{show(baseline['title'])}", "",
            f"- 建议方向：{RECOMMENDATIONS[recommendation]}",
            f"- 建议投流内容：{show(candidate['post_url'])}；首发：{show(baseline['published_at'])}",
            f"- 建议原因：{show(candidate['reason'])}", f"- 本轮假设：{show(candidate['hypothesis'])}",
            f"- 预算：{allocation}；人群、地区与周期见上方输入约束和待确认建议。",
            f"- 预期结果（建议目标，非效果承诺）：{show(candidate['expected_result']['description'])}",
            f"- 预期依据：{show(candidate['expected_result']['basis'])}",
            f"- 研究引用：{show('、'.join(candidate['research_source_refs']) or None)}", "", "数据基线：", "",
            "| 指标 | 当前值 |", "| --- | --- |"]
        for metric, label in LABELS.items():
            value = baseline.get(metric)
            display = f"{value * 100:.2f}%" if metric == "cover_click_rate" and value is not None else show(value)
            lines.append(f"| {label} | {display} |")
        for field, label in (("stop_conditions", "止损条件"), ("additional_budget_conditions", "追加预算条件（需重新审批）")):
            lines += ["", label + "：", ""]
            if not candidate[field]:
                lines.append("- 本轮未建议投流，不适用；重新考虑投流时另行制定并审批。")
            for condition in candidate[field]:
                needs = "、".join(LABELS[metric] for metric in condition["required_data"])
                missing = "、".join(LABELS[metric] for metric in condition["missing_data"]) or "无；本版仍不做投后评估"
                lines.append(f"- {show(condition['condition'])} 所需数据：{needs}；当前缺少：{missing}。尚未评估。")
        lines.append("")
    for label, values in (("待补充信息", [missing_label(value) for value in plan["missing_information"]]), ("假设", plan["assumptions"]),
                          ("数据局限与审批边界", inputs["limitations"])):
        lines += [f"## {label}", "", *["- " + show(value) for value in values], ""]
    lines += ["## 依据与版本", "", f"- 方案：{plan['plan_id']}，第 {plan['version']} 版。",
              f"- 后台采集时间：{show(inputs['metrics'].get('collected_at'))}。",
              f"- 本次保留 {len(inputs['metrics']['posts'])} 篇，后台筛选结果共 {inputs['metrics']['source_total']} 篇；不是全账号统计。",
              f"- 笔记首发筛选：{show(inputs['metrics'].get('query', {}).get('start_date'))} 至 {show(inputs['metrics'].get('query', {}).get('end_date'))}。"]
    for kind, ref in inputs["source_refs"].items():
        if ref:
            lines.append(f"- {kind}：{show(ref.get('path', ref.get('artifact_path')))}；校验值：{show(ref.get('sha256', ref.get('content_hash')))}")
    if inputs["research"]:
        lines += ["- 研究局限：" + show(item) for item in inputs["research"]["limitations"]]
        used = {ref for candidate in plan["candidates"] for ref in candidate["research_source_refs"]}
        for source in inputs["research"]["source_references"]:
            if source["source_id"] in used:
                lines.append(f"- {show(source['source_id'])}：{show(source['url'])}；采集于 {show(source['collected_at'])}。")
    if plan["previous_plan"]:
        lines += [f"- 前版：{show(plan['previous_plan']['path'])}", f"- 修订原因：{show(plan['revision_reason'])}"]
    return "\n".join(lines) + "\n"


PROMOTION_TOOL_DEFINITIONS = tuple(
    {"type": "function", "function": {"name": name, "description": description,
        "parameters": model.model_json_schema()}}
    for name, model, description in (
        ("prepare_promotion_plan", PromotionInputs, "准备投流方案：未提供 metrics_artifact_path 时，自动打开专用Chrome采集绑定账号最近30天首发的笔记（最多保留20篇），成功后冻结数据与约束。显式提供文件时复用该文件。失败后停止本轮，不重试或改用旧数据；缺业务约束也可准备，不猜预算。无付费研究或投放。"),
        ("load_promotion_plan", PromotionRead, "读取投流输入或计划。overview 看约束；goal 看原目标资源和约束；posts/research/candidates 分页看依据或方案，用 next_offset 继续，不把第一页当成全部。"),
        ("save_promotion_plan", PromotionSave, "分析输入后保存包含十项要求的投流建议。基线从冻结数据取值，校验预算、来源、版本；永远待审批，不执行投放或投后评估。修订需引用前版并说明原因。"),
    )
)
PROMOTION_TOOL_HANDLERS = {"prepare_promotion_plan": prepare_promotion_plan,
                           "load_promotion_plan": load_promotion_plan, "save_promotion_plan": save_promotion_plan}
