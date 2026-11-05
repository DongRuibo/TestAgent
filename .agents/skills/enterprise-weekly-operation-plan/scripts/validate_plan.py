#!/usr/bin/env python3
"""检查 V1.0 周计划的10项结构、选题关联和素材截止，结果只打印到终端。

不调用平台、不判断图片或研究事实、不读企业历史数据，不创建审批流程。
缺失事实给出提示，明确的结构矛盾返回失败，供 Agent 修正 Markdown。
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
from pathlib import Path
import re
import sys
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SECTIONS = ["本周目标", "各平台选题", "发布时间", "素材需求", "人类协作人",
            "素材协作截止时间", "预期数据", "成功或失败判断规则", "备选选题", "调整机制"]
UNKNOWN = {"", "待确认", "待补充", "未提供", "待补充提交人", "上游未提供", "暂无法估算"}


def tables(text: str, required: set[str]) -> list[dict[str, str]]:
    """提取具有指定表头的表格，保留转义竖线和中文选题名称。"""
    result, header = [], None
    for raw in text.splitlines():
        line = raw.strip()
        if not (line.startswith("|") and line.endswith("|")):
            header = None
            continue
        values = [value.strip().replace(r"\|", "|")
                  for value in re.split(r"(?<!\\)\|", line[1:-1])]
        if all(re.fullmatch(r":?-{3,}:?", value) for value in values):
            continue
        if required.issubset(values):
            header = values
        elif header:
            if len(values) != len(header):
                raise ValueError("表格列数不一致，请检查竖线转义")
            result.append(dict(zip(header, values)))
    return result


def sections(text: str) -> dict[str, str]:
    """按二级标题读取章节，兼容 1. 或 3.1 等编号，不依赖编号格式。"""
    result = {}
    matches = list(re.finditer(r"^##\s+(.+?)\s*$", text, re.M))
    for index, match in enumerate(matches):
        name = re.sub(r"^\d+(?:\.\d+)*[.、]?\s*", "", match[1])
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        if name in result:
            raise ValueError(f"章节重复：{name}")
        result[name] = text[match.end():end].strip()
    return result


def require_rows(block: str, fields: set[str], name: str, errors: list[str]) -> list[dict[str, str]]:
    """取得执行表格；缺少必要表头或没有内容时给出明确错误。"""
    result = tables(block, fields)
    if not result:
        errors.append(f"{name}缺少可读取的表格，必要列：{'、'.join(sorted(fields))}")
    for row in result:
        for field in fields:
            if not row[field]:
                errors.append(f"{name}的{field}不能为空")
    return result


def validate(text: str, *, now: datetime | None = None) -> tuple[list[str], list[str]]:
    """验证本周计划可关联的执行信息；不把未知信息当成已确认事实。"""
    errors, notes = [], []
    blocks = sections(text)
    for name in SECTIONS:
        if not blocks.get(name):
            errors.append(f"缺少章节：{name}")
    if re.search(r"\{\{[^}]+\}\}", text):
        errors.append("仍有未替换的模板占位符")
    for name in blocks:
        if re.search(r"审批|审核记录|置信度|投流建议|数据依据摘要", name):
            errors.append(f"请移除不属于本版本输出的章节：{name}")
    if re.search(r"\|\s*(?:计划状态|审批状态|审核状态|置信度)\s*\|", text):
        errors.append("本版本不输出计划审批状态或置信度字段")

    goals = blocks.get("本周目标", "")
    goal_rows = require_rows(goals, {"项目", "安排"}, "本周目标", errors)
    for label in ("长周期目标", "本周阶段目标", "目标用户", "产品重点", "用户需求", "内容形式"):
        if label not in {row["项目"] for row in goal_rows}:
            errors.append(f"本周目标缺少：{label}")
    form = next((row["安排"] for row in goal_rows if row["项目"] == "内容形式"), "")
    if form != "图文":
        errors.append("当前内容形式应为图文")

    topics = require_rows(blocks.get("各平台选题", ""),
                          {"选题", "平台", "内容形式", "内容角度", "面向用户", "研究依据", "来源ID"},
                          "各平台选题", errors)
    names = [row["选题"] for row in topics]
    if len(set(names)) != len(names) or any(name in UNKNOWN for name in names):
        errors.append("主选题名称为空或重复")
    for row in topics:
        if row["平台"] not in {"小红书", "抖音"}:
            errors.append(f"不支持的平台：{row['平台']}")
        if row["内容形式"] != "图文":
            errors.append(f"{row['选题']}的内容形式应明确为图文")

    schemas = {
        "发布时间": {"选题", "终稿截止时间", "发布日期", "发布时间"},
        "素材需求": {"选题", "图片需求", "数量和画面要求"},
        "人类协作人": {"选题", "姓名", "岗位", "提交素材", "负责事项"},
        "素材协作截止时间": {"选题", "提交人", "素材截止时间"},
    }
    data = {}
    for name, fields in schemas.items():
        items = require_rows(blocks.get(name, ""), fields, name, errors)
        data[name] = items
        seen = {row["选题"] for row in items}
        if seen != set(names):
            errors.append(f"{name}的选题与主选题不一致：请补齐缺项并移除多余选题")

    header = re.split(r"^##\s+", text, maxsplit=1, flags=re.M)[0]
    meta = {row["字段"]: row["内容"] for row in tables(header, {"字段", "内容"})}
    for field in ("企业", "计划ID", "计划周期", "时区", "生成时间"):
        if meta.get(field, "") in UNKNOWN:
            errors.append(f"计划元信息缺少：{field}")
    plan_id = meta.get("计划ID", "")
    try:
        if str(UUID(plan_id)) != plan_id or UUID(plan_id).int == 0:
            raise ValueError
    except ValueError:
        errors.append("计划ID应为标准非空UUID")
    try:
        zone = ZoneInfo(meta.get("时区", ""))
    except (ZoneInfoNotFoundError, ValueError):
        errors.append("时区应为有效IANA名称，例如Asia/Shanghai")
        zone = timezone.utc
    current = now if now is not None else datetime.now(timezone.utc)
    if current.utcoffset() is None:
        raise ValueError("校验时钟必须带时区")
    current = current.astimezone(zone)
    try:
        generated = datetime.fromisoformat(meta.get("生成时间", ""))
        if generated.utcoffset() is None:
            raise ValueError
        if generated > current:
            errors.append("生成时间不能晚于校验时钟")
    except ValueError:
        errors.append("生成时间应为带时区的ISO日期时间")
    dates = re.findall(r"\d{4}-\d{2}-\d{2}", meta.get("计划周期", ""))
    bounds = None
    try:
        if len(dates) != 2:
            raise ValueError
        bounds = tuple(date.fromisoformat(value) for value in dates)
        if not 0 <= (bounds[1] - bounds[0]).days <= 6:
            raise ValueError
    except ValueError:
        errors.append("计划周期应是明确且不超过7天的起止日期")

    expected_content_prefix = None
    if bounds:
        iso_year, iso_week, _ = bounds[0].isocalendar()
        expected_content_prefix = f"{plan_id}:{iso_year}-W{iso_week:02d}-C"

    publish, final_drafts = {}, {}
    for row in data["发布时间"]:
        name = row["选题"]
        if name in publish:
            errors.append(f"{name}有重复发布时间")
        raw = f"{row['发布日期']} {row['发布时间']}"
        try:
            stamp = datetime.strptime(raw, "%Y-%m-%d %H:%M").replace(tzinfo=zone)
            if stamp <= current:
                errors.append(f"{name}的发布时间已过或不晚于当前时间")
            if bounds and not bounds[0] <= stamp.date() <= bounds[1]:
                errors.append(f"{name}的发布时间不在计划周期内")
            publish[name] = stamp
        except ValueError:
            if row["发布日期"] in UNKNOWN or row["发布时间"] in UNKNOWN:
                errors.append(f"{name}尚缺可执行的发布时间")
            else:
                errors.append(f"{name}发布时间格式应为 YYYY-MM-DD 和 HH:MM")
        final_raw = row["终稿截止时间"]
        if final_raw in UNKNOWN:
            errors.append(f"{name}缺少具体终稿截止时间")
        else:
            try:
                final_stamp = datetime.strptime(final_raw, "%Y-%m-%d %H:%M").replace(tzinfo=zone)
                if final_stamp <= current:
                    errors.append(f"{name}的终稿截止已过或不晚于当前时间")
                final_drafts[name] = final_stamp
                if name in publish and final_stamp >= publish[name]:
                    errors.append(f"{name}的终稿截止必须早于发布时间")
            except ValueError:
                errors.append(f"{name}的终稿截止应为 YYYY-MM-DD HH:MM")

    # 每种素材独立一行，使用显式列精确关联，不猜测职责文字。
    materials = [(row["选题"], row["图片需求"]) for row in data["素材需求"]]
    for row in data["素材需求"]:
        if row["图片需求"] in UNKNOWN or row["数量和画面要求"] in UNKNOWN:
            errors.append("素材需求必须有具体图片名称及数量和画面要求")
    if len(materials) != len(set(materials)):
        errors.append("同一选题的素材名称不能重复，请合并数量要求")
    assignments = [row for row in data["人类协作人"] if row["提交素材"] != "—"]
    assigned = {(row["选题"], row["提交素材"]) for row in assignments}
    if assigned != set(materials):
        errors.append("提交素材必须逐项精确对应素材需求，不能缺项或增加未声明素材")
    assignment_keys = [(row["选题"], row["姓名"], row["提交素材"]) for row in assignments]
    if len(assignment_keys) != len(set(assignment_keys)):
        errors.append("同一提交人的素材分工不能重复")
    providers = {(row["选题"], row["姓名"]) for row in assignments}
    deadline_rows = data["素材协作截止时间"]
    deadlines = [(row["选题"], row["提交人"]) for row in deadline_rows]
    if providers != set(deadlines) or len(deadlines) != len(set(deadlines)):
        errors.append("素材截止须与每个选题的提交人一一对应，不能缺失或重复")
    for topic, person in providers:
        if person in UNKNOWN:
            notes.append(f"{topic}缺少素材提交人，分派时需要补充")
    for row in deadline_rows:
        name, raw = row["选题"], row["素材截止时间"]
        if raw in UNKNOWN:
            errors.append(f"{name}尚缺具体素材截止时间")
            continue
        try:
            deadline = datetime.strptime(raw, "%Y-%m-%d %H:%M").replace(tzinfo=zone)
            if deadline <= current:
                errors.append(f"{name}的素材截止已过或不晚于当前时间")
            if name in final_drafts and deadline >= final_drafts[name]:
                errors.append(f"{name}的素材截止必须早于终稿截止")
            elif name in publish and deadline >= publish[name]:
                errors.append(f"{name}的素材截止必须早于发布")
        except ValueError:
            errors.append(f"{name}的素材截止应为 YYYY-MM-DD HH:MM")

    rules = blocks.get("成功或失败判断规则", "")
    if not all(re.search(rf"^###\s+{label}\s*$", rules, re.M) for label in ("发布前", "发布后")):
        errors.append("成功或失败判断规则需要分别包含发布前、发布后")
    expected = require_rows(blocks.get("预期数据", ""),
                            {"对象", "指标或预期动作", "上游本周目标", "同类帖子参考", "本次预期", "观察窗口"},
                            "预期数据", errors)
    rule_parts = re.split(r"^###\s+发布后\s*$", rules, flags=re.M)
    pre = require_rows(rule_parts[0],
                       {"对象", "可以继续的条件", "出现问题的情形", "处理方式"}, "发布前", errors)
    post = require_rows(rule_parts[1] if len(rule_parts) == 2 else "",
                        {"对象", "观察窗口", "成功或达到预期", "未达到预期", "处理方式"}, "发布后", errors)
    for label, rows in (("预期数据", expected), ("发布前", pre), ("发布后", post)):
        objects = {row["对象"] for row in rows}
        if objects - set(names) - {"本周整体", "全部主选题"}:
            errors.append(f"{label}存在未声明的对象")
        if not objects & {"本周整体", "全部主选题"} and not set(names) <= objects:
            errors.append(f"{label}未覆盖全部主选题")
    for row in post:
        relevant = [item for item in expected if item["对象"] == row["对象"]
                    or item["对象"] in {"本周整体", "全部主选题"}
                    or row["对象"] in {"本周整体", "全部主选题"}]
        if row["观察窗口"] not in {item["观察窗口"] for item in relevant}:
            errors.append(f"{row['对象']}发布后观察窗口与预期数据不一致")
    backup = blocks.get("备选选题", "")
    if "本次暂无适用备选" not in backup:
        for row in require_rows(backup, {"备选选题", "平台", "替代对象", "启用条件", "图片与人员安排"}, "备选选题", errors):
            if row["替代对象"] not in names:
                errors.append("备选选题的替代对象不是主选题")
            if row["平台"] not in {item["平台"] for item in topics if item["选题"] == row["替代对象"]}:
                errors.append("备选选题平台应与替代对象一致")
    for row in require_rows(blocks.get("调整机制", ""), {"触发情况", "调整动作", "影响选题", "需要谁配合"}, "调整机制", errors):
        if row["影响选题"] not in set(names) | {"全部主选题", "本周整体"}:
            errors.append("调整机制的影响选题不是主选题")
    links = require_rows(blocks.get("系统关联表", ""), {"正文名称", "系统ID", "类型", "回查位置"}, "系统关联表", errors)
    for kind in ("运营目标", "研究结果"):
        refs = [row for row in links if row["类型"] == kind]
        if not refs:
            errors.append(f"系统关联表缺少{kind}")
        elif any(row["系统ID"] in UNKNOWN for row in refs):
            notes.append(f"{kind}缺少上游ID，需要按回查位置核对来源")
    source_rows = [row for row in links if row["类型"] == "来源"]
    if any(row["回查位置"] in UNKNOWN for row in source_rows):
        errors.append("来源缺少可回查位置")
    source_ids = {row["系统ID"] for row in source_rows}
    if len(source_ids) != len(source_rows):
        errors.append("来源ID重复")
    for row in topics:
        refs = {value.strip() for value in row["来源ID"].split("；")}
        if refs & UNKNOWN or not refs <= source_ids:
            errors.append(f"{row['选题']}的来源ID缺失或无法回查")
    main = [row for row in links if row["类型"] == "本周内容"]
    if {row["正文名称"] for row in main} != set(names) or len(main) != len(names):
        errors.append("系统关联表须包含每个主选题且仅关联一次")
    ids = [row["系统ID"] for row in main]
    if len(set(ids)) != len(ids) or any(value in UNKNOWN for value in ids):
        errors.append("主选题内容ID为空或重复")
    elif any(not re.fullmatch(re.escape(plan_id) + r":\d{4}-W(?:0[1-9]|[1-4]\d|5[0-3])-C\d{2,}", value)
             for value in ids):
        errors.append("主选题内容ID应使用 计划ID:YYYY-Www-Cnn 格式")
    elif expected_content_prefix and any(not value.startswith(expected_content_prefix) for value in ids):
        errors.append(f"主选题内容ID应以计划开始日期所在周 {expected_content_prefix} 开头")
    elif any(int(value.rsplit("-C", 1)[1]) == 0 for value in ids):
        errors.append("主选题序号应从C01起")
    return list(dict.fromkeys(errors)), list(dict.fromkeys(notes))


def main() -> int:
    """读取指定 Markdown，打印检查结果；不写文件、不改变计划状态。"""
    parser = argparse.ArgumentParser(description="检查 V1.1 企业周运营计划（不验证研究事实或宿主权限）")
    parser.add_argument("--markdown", type=Path, required=True)
    parser.add_argument("--now", help="仅供离线测试/历史回放：带时区的ISO日期时间，默认真实当前时间")
    args = parser.parse_args()
    try:
        now = datetime.fromisoformat(args.now) if args.now else None
        errors, notes = validate(args.markdown.read_text(encoding="utf-8-sig"), now=now)
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"无法检查：{exc}", file=sys.stderr)
        return 1
    for item in errors:
        print(f"需要修正：{item}")
    for item in notes:
        print(f"信息待补：{item}")
    if not errors:
        print("结构与排期检查通过；存在待补信息，不可直接分派。" if notes
              else "结构、关联与排期检查通过；研究事实和业务可行性仍需核实。")
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
