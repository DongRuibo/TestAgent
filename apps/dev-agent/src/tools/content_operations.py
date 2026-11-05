"""柒昱美学设计公司获客内容运营 V0 的确定性业务工具。"""

from __future__ import annotations

import csv
import ipaddress
import json
from copy import deepcopy
from datetime import date
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid5

from testagent_dev_agent.paths import REPOSITORY_ROOT
from tools.content_research import load_content_research_artifact
from tools.direct_spreadsheet import _resolve_spreadsheet


CONTENT_WEEKLY_PLAN_TOOL_NAME = "generate_content_weekly_plan"
CONTENT_PERFORMANCE_TOOL_NAME = "analyze_content_performance"
CONTENT_WEEKLY_PLAN_SCENARIO = "content_weekly_plan"
CONTENT_PERFORMANCE_SCENARIO = "content_performance_review"
CONTENT_FIXTURE_DIR = REPOSITORY_ROOT / "fixtures" / "content_operations_v0"
CONTENT_OUTPUT_DIR = REPOSITORY_ROOT / "artifacts" / "content_operations"


def _tool(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str],
) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


GENERATE_CONTENT_WEEKLY_PLAN_TOOL = _tool(
    CONTENT_WEEKLY_PLAN_TOOL_NAME,
    (
        "为柒昱美学设计公司生成一周 5 个小红书图文选题，同时生成短视频脚本、"
        "标签、参考来源和缺失素材清单。真实公开研究应先调用 web_search，并把搜索候选"
        "原样放入 source_items；不提供时使用明确标记的离线演示线索。"
    ),
    {
        "week_start": {"type": "string", "description": "周一日期，YYYY-MM-DD。"},
        "source_items": {
            "type": "array",
            "maxItems": 5,
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "maxLength": 200},
                    "url": {"type": "string", "maxLength": 2000},
                    "summary": {"type": "string", "maxLength": 1000},
                },
                "required": ["title", "url", "summary"],
                "additionalProperties": False,
            },
        },
        "research_artifact_path": {
            "type": "string",
            "maxLength": 2000,
            "description": (
                "可选的 14.2 研究 JSON 工件绝对路径。提供后只生成待确认的 14.3 周计划，"
                "不会制作正文或启动 RPA。"
            ),
        },
    },
    ["week_start"],
)

ANALYZE_CONTENT_PERFORMANCE_TOOL = _tool(
    CONTENT_PERFORMANCE_TOOL_NAME,
    (
        "分析 workspace 或 fixtures 中的平台表现 CSV，按确定性规则计算互动、咨询和线索表现，"
        "输出继续观察、调整内容、建议小额投流或建议停止；只给建议，不执行投流。"
    ),
    {"path": {"type": "string", "description": "平台表现 CSV 相对路径。"}},
    ["path"],
)

CONTENT_OPERATION_TOOL_DEFINITIONS = (
    GENERATE_CONTENT_WEEKLY_PLAN_TOOL,
    ANALYZE_CONTENT_PERFORMANCE_TOOL,
)


class PresentationRegistry(Protocol):
    def register(self, scenario: str, data: dict[str, Any]) -> dict[str, Any]: ...


class ContentOperationTools:
    def __init__(self, registry: PresentationRegistry) -> None:
        self.registry = registry

    def generate_weekly_plan(
        self,
        week_start: str,
        source_items: list[dict[str, Any]] | None = None,
        research_artifact_path: str | None = None,
    ) -> dict[str, Any]:
        try:
            data = generate_content_weekly_plan(
                week_start,
                source_items=source_items,
                research_artifact_path=research_artifact_path,
            )
            artifacts = _write_artifacts("weekly-plan", week_start, data)
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            return _error("INVALID_CONTENT_WEEKLY_PLAN", str(exc))
        result = {
            "success": True,
            "workflow": (
                "content_weekly_plan_from_research_v0"
                if research_artifact_path
                else "content_weekly_plan_v0"
            ),
            "plan": data,
            "artifacts": artifacts,
            "message": f"{week_start} 起的一周内容计划已生成，共 {len(data['contents'])} 个选题。",
        }
        if research_artifact_path is None:
            result["presentation"] = self.registry.register(
                CONTENT_WEEKLY_PLAN_SCENARIO, data
            )
        return result

    def analyze_performance(self, path: str) -> dict[str, Any]:
        try:
            data = analyze_content_performance(path)
            artifacts = _write_artifacts(
                "performance-review", data["period_label"], data
            )
        except (OSError, UnicodeError, csv.Error, ValueError) as exc:
            return _error("INVALID_CONTENT_PERFORMANCE", str(exc))
        return {
            "success": True,
            "workflow": "content_performance_review_v0",
            "review": data,
            "artifacts": artifacts,
            "presentation": self.registry.register(CONTENT_PERFORMANCE_SCENARIO, data),
            "message": f"已分析 {data['metrics']['content_count']} 条内容表现。",
        }

    def handlers(self) -> dict[str, Any]:
        return {
            CONTENT_WEEKLY_PLAN_TOOL_NAME: self.generate_weekly_plan,
            CONTENT_PERFORMANCE_TOOL_NAME: self.analyze_performance,
        }


def generate_content_weekly_plan(
    week_start: str,
    *,
    source_items: list[dict[str, Any]] | None = None,
    research_artifact_path: str | None = None,
) -> dict[str, Any]:
    start = _monday(week_start)
    if research_artifact_path is not None:
        if source_items is not None:
            raise ValueError("source_items 和 research_artifact_path 不能同时提供。")
        research = load_content_research_artifact(research_artifact_path)
        return _weekly_plan_from_research(start, research, research_artifact_path)
    sources = _normalize_sources(source_items)
    source_mode = "公开搜索候选" if sources else "离线演示线索"
    if not sources:
        decoded = json.loads(
            (CONTENT_FIXTURE_DIR / "industry_signals.json").read_text("utf-8")
        )
        sources = _normalize_sources(decoded)
    plan_id = str(uuid5(NAMESPACE_URL, f"柒昱美学设计公司:{start.isoformat()}"))
    templates = _content_templates()
    contents = []
    for index, template in enumerate(templates, 1):
        source = sources[(index - 1) % len(sources)]
        content_id = f"CO-{start.strftime('%Y%m%d')}-{index:02d}"
        contents.append(
            {
                "content_id": content_id,
                "platform": "小红书图文",
                "publish_day": template["publish_day"],
                "audience": template["audience"],
                "goal": template["goal"],
                "title": template["title"],
                "body": template["body"],
                "tags": template["tags"],
                "tags_text": " ".join(f"#{tag}" for tag in template["tags"]),
                "video_script": template["video_script"],
                "missing_assets": template["missing_assets"],
                "missing_assets_text": "；".join(template["missing_assets"]),
                "source_title": source["title"],
                "source_url": source["url"],
                "source_summary": source["summary"],
                "status": "pending_review",
                "status_label": "待审核",
                "review_note": "",
                "image_paths": [],
                "approved_at": "",
                "rpa_status": "not_started",
                "rpa_status_label": "未执行",
                "rpa_job_id": "",
                "rpa_message": "",
            }
        )
    return {
        "schema_version": "1.0",
        "plan_id": plan_id,
        "company_name": "柒昱美学设计公司",
        "week_start": start.isoformat(),
        "platform": "小红书图文优先，附短视频通用脚本",
        "source_mode": source_mode,
        "source_notice": (
            "来源为搜索候选，仅用于选题启发；涉及具体事实时需继续打开原网页核实。"
            if source_mode == "公开搜索候选"
            else "当前使用离线模拟线索，仅用于产品验收，不代表实时行业事实。"
        ),
        "summary": "本周生成 5 个获客选题；发布与投流均需人工确认。",
        "metrics": {
            "content_count": len(contents),
            "pending_review_count": len(contents),
            "approved_count": 0,
            "draft_saved_count": 0,
        },
        "contents": contents,
        "reference_sources": sources,
        "rpa_boundary": "RPA 只能把已审核图文保存到小红书草稿箱，不执行最终发布。",
    }


def _weekly_plan_from_research(
    start: date,
    research: dict[str, Any],
    research_artifact_path: str,
) -> dict[str, Any]:
    if research["execution_status"] == "failed":
        raise ValueError("失败的 14.2 研究工件不能进入周计划。")
    candidates = research["candidate_topics"]
    selected = candidates[:5]
    if not selected:
        raise ValueError("14.2 研究工件没有可用候选选题。")
    weekdays = ("周一", "周二", "周三", "周四", "周五")
    contents = []
    for index, topic in enumerate(selected, 1):
        contents.append(
            {
                "content_id": f"CO-{start.strftime('%Y%m%d')}-{index:02d}",
                "platform": "小红书",
                "publish_day": weekdays[index - 1],
                "content_format": "小红书图文",
                "audience": topic["target_user"],
                "goal": "；".join(topic["expected_result"]),
                "title": topic["title"],
                "evidence": deepcopy(topic["evidence"]),
                "why_fit_company": topic["why_fit_company"],
                "required_materials": deepcopy(topic["required_materials"]),
                "risks": deepcopy(topic["risks"]),
                "ratings": deepcopy(topic["ratings"]),
                "recommendation": topic["recommendation"],
                "human_collaborator": "待负责人指定",
                "delivery_time": "待负责人确认",
                "status": "pending_confirmation",
                "status_label": "待负责人确认",
            }
        )
    plan_id = str(
        uuid5(
            NAMESPACE_URL,
            f"{research['research_id']}:{start.isoformat()}:weekly-plan",
        )
    )
    return {
        "schema_version": "1.2",
        "plan_id": plan_id,
        "company_name": research["company_name"],
        "week_start": start.isoformat(),
        "platform": "小红书",
        "weekly_goal": research["operation_goal"],
        "target_users": list(dict.fromkeys(item["audience"] for item in contents)),
        "core_themes": [item["title"] for item in contents],
        "source_mode": "14.2 结构化研究工件",
        "source_notice": (
            "选题来自可追溯的 14.2 研究工件；事实仍须按来源核实，计划和选题必须由负责人确认。"
        ),
        "summary": (
            f"已从 14.2 研究结果选择 {len(contents)} 个小红书候选选题形成周计划草案。"
        ),
        "approval_status": "pending_confirmation",
        "metrics": {
            "content_count": len(contents),
            "pending_confirmation_count": len(contents),
        },
        "contents": contents,
        "backup_topics": deepcopy(candidates[5:]),
        "reference_sources": deepcopy(research["source_references"]),
        "research_id": research["research_id"],
        "research_artifact_path": research_artifact_path,
        "research_ref": {
            "research_id": research["research_id"],
            "version": research["version"],
            "schema_version": research["schema_version"],
            "artifact_path": research_artifact_path,
        },
        "company_profile": research["company_profile"],
        "goal_ref": deepcopy(research["goal_ref"]),
        "goal_snapshot": deepcopy(research["goal_snapshot"]),
        "research_input_mode": research["input_mode"],
        "research_execution_status": research["execution_status"],
        "research_approval_status": research["approval_status"],
        "research_limitations": deepcopy(research["limitations"]),
        "success_failure_rule": "沿用 14.1 运营目标中已确认的衡量方法，不在 14.3 重定义指标。",
        "adjustment_mechanism": "负责人确认后执行；素材或事实不足时替换为备选选题并重新确认。",
        "rpa_boundary": "当前只生成 14.3 周计划草案，尚未制作正文，不能启动 RPA。",
    }


def analyze_content_performance(path: str) -> dict[str, Any]:
    source = _resolve_spreadsheet(path)
    if source.suffix.casefold() != ".csv":
        raise ValueError("内容表现 V0 只支持 CSV。")
    required = {
        "content_id", "title", "impressions", "likes", "comments",
        "saves", "inquiries", "leads", "spend",
    }
    rows: list[dict[str, Any]] = []
    with source.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or required - set(reader.fieldnames):
            missing = "、".join(sorted(required - set(reader.fieldnames or ())))
            raise ValueError(f"内容表现 CSV 缺少字段：{missing}。")
        for line, raw in enumerate(reader, 2):
            values = {
                key: _nonnegative_number(raw.get(key), f"第 {line} 行 {key}")
                for key in (
                    "impressions", "likes", "comments", "saves",
                    "inquiries", "leads", "spend",
                )
            }
            impressions = values["impressions"]
            engagement = values["likes"] + values["comments"] + values["saves"]
            engagement_rate = engagement / impressions if impressions else 0
            inquiry_rate = values["inquiries"] / impressions if impressions else 0
            lead_rate = values["leads"] / impressions if impressions else 0
            decision, reason = _performance_decision(
                impressions,
                engagement_rate,
                values["inquiries"],
                values["leads"],
                values["spend"],
            )
            rows.append(
                {
                    "content_id": _required_text(raw.get("content_id"), "content_id", 80),
                    "title": _required_text(raw.get("title"), "title", 200),
                    **values,
                    "engagement_rate": round(engagement_rate, 4),
                    "inquiry_rate": round(inquiry_rate, 4),
                    "lead_rate": round(lead_rate, 4),
                    "decision": decision,
                    "reason": reason,
                }
            )
    if not rows:
        raise ValueError("内容表现 CSV 没有数据。")
    decision_counts = {
        decision: sum(item["decision"] == decision for item in rows)
        for decision in ("建议小额投流", "继续观察", "调整内容", "建议停止")
    }
    return {
        "schema_version": "1.0",
        "company_name": "柒昱美学设计公司",
        "period_label": source.stem,
        "source_file": source.name,
        "summary": "已按曝光、互动、咨询、线索和已有花费生成确定性建议；不会自动投流。",
        "metrics": {
            "content_count": len(rows),
            "total_impressions": sum(item["impressions"] for item in rows),
            "total_inquiries": sum(item["inquiries"] for item in rows),
            "total_leads": sum(item["leads"] for item in rows),
            **decision_counts,
        },
        "contents": rows,
        "next_week_actions": [
            "优先复用产生有效线索的清单型和避坑型选题。",
            "对高曝光低咨询内容重写首屏标题和行动引导。",
            "任何投流建议都必须由负责人确认预算后执行。",
        ],
        "spend_boundary": "本工具只给投流建议，不创建订单、不扣费。",
    }


def compact_content_operation_result(
    result: dict[str, Any],
    *,
    ui_result: dict[str, Any],
) -> dict[str, Any]:
    if result.get("success") is not True:
        return deepcopy(result)
    response = {
        key: deepcopy(result[key])
        for key in ("success", "workflow", "artifacts", "message")
        if key in result
    }
    response.update(
        {
            "compact_result_complete": True,
            "ui_presented": ui_result.get("success") is True,
            "ui": deepcopy(ui_result),
        }
    )
    if isinstance(result.get("plan"), dict):
        plan = result["plan"]
        response["plan"] = {
            "company_name": plan["company_name"],
            "week_start": plan["week_start"],
            "source_mode": plan["source_mode"],
            "source_notice": plan["source_notice"],
            "summary": plan["summary"],
            "contents": [
                {
                    key: item[key]
                    for key in (
                        "content_id", "publish_day", "title", "goal",
                        "missing_assets_text", "source_title", "source_url",
                        "status_label",
                    )
                }
                for item in plan["contents"]
            ],
            "rpa_boundary": plan["rpa_boundary"],
        }
    if isinstance(result.get("review"), dict):
        response["review"] = deepcopy(result["review"])
    return response


def _content_templates() -> list[dict[str, Any]]:
    return [
        {
            "publish_day": "周一", "audience": "首次装修、担心增项的业主", "goal": "收藏与私信咨询",
            "title": "装修报价最容易漏掉的5项",
            "body": "同样写着全包，最后总价为什么差很多？真正拉开预算的，往往是报价单里没有说清的拆改、找平、收口、运输和成品保护。签约前逐项确认计价单位、数量和增项条件，比只看总价更重要。需要报价核对清单，可以私信我们。",
            "tags": ["杭州装修", "装修报价", "装修避坑", "柒昱美学"],
            "video_script": "开场：同样100㎡为什么报价差5万？｜镜头：报价单局部与现场收口｜讲解：依次展示5类漏项｜结尾：评论区领取核对清单。",
            "missing_assets": ["一份脱敏报价单局部", "2张真实收口节点照片", "设计师口播视频"],
        },
        {
            "publish_day": "周二", "audience": "准备开工的改善型业主", "goal": "建立专业信任",
            "title": "开工前必须确认的4件事",
            "body": "开工不是工人进场就算开始。图纸版本、材料到场、物业手续和现场责任人，只要有一项没确认，后续都可能变成停工和返工。我们会在开工前做一次清单式确认，让客户知道谁负责、什么时候完成、出现变化怎么同步。",
            "tags": ["杭州装修设计", "装修开工", "施工管理", "柒昱美学"],
            "video_script": "开场：今天开工，为什么我们反而先不敲墙？｜镜头：开工交底现场｜讲解：图纸、材料、物业、责任人四项确认｜结尾：展示签字清单。",
            "missing_assets": ["开工交底现场横竖屏视频", "交底清单脱敏截图", "项目经理出镜"],
        },
        {
            "publish_day": "周三", "audience": "预算敏感但重视品质的业主", "goal": "纠正价格认知",
            "title": "预算超支真不一定是材料贵",
            "body": "装修超预算，常见原因不是某一块砖贵了，而是方案反复、尺寸未复核、项目漏项和临时变更叠加。设计阶段把生活需求和施工边界讲清楚，施工阶段每次变更都留痕，预算才真正可控。",
            "tags": ["装修预算", "全案设计", "装修经验", "柒昱美学"],
            "video_script": "开场：预算超支，先别怪材料｜镜头：变更单与现场复尺｜讲解：四类隐性成本｜结尾：给出预算控制三步法。",
            "missing_assets": ["变更单脱敏案例", "现场复尺视频", "预算前后对比图"],
        },
        {
            "publish_day": "周四", "audience": "关注工期的在职家庭", "goal": "解释项目管理价值",
            "title": "100㎡工期为何能差一个月",
            "body": "面积相同，工期也可能完全不同。拆改量、定制品周期、工种衔接和验收返工都会影响交付。靠谱的工期表不是写一个完工日期，而是把前置条件、责任人和风险节点都列清楚。",
            "tags": ["装修工期", "项目管理", "杭州装修公司", "柒昱美学"],
            "video_script": "开场：100㎡装修到底要多久？｜镜头：施工排期表｜讲解：四个决定工期的变量｜结尾：展示节点同步方式。",
            "missing_assets": ["脱敏施工排期表", "三个工种衔接镜头", "完工案例照片"],
        },
        {
            "publish_day": "周五", "audience": "经历过装修纠纷的改善客户", "goal": "突出服务与闭环",
            "title": "装修投诉高发的3个节点",
            "body": "客户不满意往往不是问题本身，而是问题发生后没人回应、没人负责、没有明确关闭时间。设计确认、隐蔽验收和交付收口是三个高频节点。我们更看重问题是否被记录、分派、跟进并真正关闭。",
            "tags": ["装修售后", "装修避坑指南", "施工验收", "柒昱美学"],
            "video_script": "开场：装修投诉为什么越拖越大？｜镜头：项目群沟通与验收单｜讲解：三个高频节点｜结尾：展示问题闭环流程。",
            "missing_assets": ["脱敏沟通记录", "隐蔽验收照片", "交付收口前后对比"],
        },
    ]


def _normalize_sources(value: Any) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list) or not 1 <= len(value) <= 5:
        raise ValueError("source_items 必须包含 1-5 个搜索候选。")
    normalized = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("source_items 每项必须是对象。")
        url = _public_url(item.get("url"))
        normalized.append(
            {
                "title": _required_text(item.get("title"), "source title", 200),
                "url": url,
                "summary": _required_text(item.get("summary"), "source summary", 1000),
            }
        )
    return normalized


def _public_url(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 2000:
        raise ValueError("来源 URL 无效。")
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("来源 URL 必须是 HTTP(S) 公网地址。")
    host = parsed.hostname.casefold()
    if host == "localhost" or host.endswith(".local"):
        raise ValueError("来源 URL 不能是本机或内网地址。")
    try:
        if ipaddress.ip_address(host).is_private:
            raise ValueError("来源 URL 不能是本机或内网地址。")
    except ValueError as exc:
        if str(exc) == "来源 URL 不能是本机或内网地址。":
            raise
    return value.strip()


def _monday(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("week_start 必须是 YYYY-MM-DD。") from exc
    if parsed.weekday() != 0:
        raise ValueError("week_start 必须是周一。")
    return parsed


def _required_text(value: Any, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
        raise ValueError(f"{name} 必须是 1-{limit} 个字符。")
    return value.strip()


def _nonnegative_number(value: Any, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是非负数字。") from exc
    if number < 0:
        raise ValueError(f"{name} 必须是非负数字。")
    return number


def _performance_decision(
    impressions: float,
    engagement_rate: float,
    inquiries: float,
    leads: float,
    spend: float,
) -> tuple[str, str]:
    if leads >= 3 and inquiries >= 5 and engagement_rate >= 0.05:
        return "建议小额投流", "自然流量已产生稳定咨询和线索；投流前仍需人工确认预算。"
    if impressions < 1500:
        return "继续观察", "曝光样本不足，先继续积累自然流量。"
    if impressions >= 5000 and engagement_rate < 0.01 and leads == 0:
        return "建议停止", "曝光较高但互动和线索过低，继续投入价值有限。"
    if engagement_rate < 0.04 or inquiries == 0 or (spend > 0 and leads == 0):
        return "调整内容", "需要调整标题、首屏表达或咨询引导后再观察。"
    return "继续观察", "已有一定互动或咨询，暂不扩大投入。"


def _write_artifacts(kind: str, label: str, data: dict[str, Any]) -> list[dict[str, Any]]:
    safe_label = label.replace("/", "-")
    target = CONTENT_OUTPUT_DIR / kind / safe_label
    target.mkdir(parents=True, exist_ok=True)
    json_path = target / f"{kind}.json"
    json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", "utf-8")
    stored = json_path.resolve().relative_to(REPOSITORY_ROOT.resolve()).as_posix()
    return [{
        "kind": kind,
        "path": stored,
        "media_type": "application/json",
        "size_bytes": json_path.stat().st_size,
    }]


def _error(error_type: str, message: str) -> dict[str, Any]:
    return {
        "success": False,
        "error_type": error_type,
        "message": message,
        "retryable": False,
    }


__all__ = [
    "CONTENT_OPERATION_TOOL_DEFINITIONS",
    "CONTENT_PERFORMANCE_SCENARIO",
    "CONTENT_PERFORMANCE_TOOL_NAME",
    "CONTENT_WEEKLY_PLAN_SCENARIO",
    "CONTENT_WEEKLY_PLAN_TOOL_NAME",
    "ContentOperationTools",
    "analyze_content_performance",
    "compact_content_operation_result",
    "generate_content_weekly_plan",
]
