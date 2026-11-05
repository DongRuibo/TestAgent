"""运营目标/研究的文本展示与显式聊天操作；不让模型判断执行授权。"""

import json
import re
from datetime import date
from urllib.parse import quote
from uuid import uuid4

from tools.operation_goals import GOAL_SCENARIO, OperationGoalStore
from web_agent.content_workflow import WorkflowAction
from web_agent.conversations import GenerationUiActionConflictError
from web_agent.weekly_planning import weekly_reply
from web_agent.research_session import research_limits
from web_agent.business_intents import goal_confirmation


def plain(value):
    """业务字段作为文字呈现，不能注入 Markdown 链接或 HTML。"""
    text = str(value if value is not None else "未知")
    return re.sub(r"([\\`*_{}\[\]<>|#])", r"\\\1", text).replace("\n", " ")


def goal_text(data, *, historical=False, full=False):
    if not full:
        return goal_brief(data, historical=historical)
    snapshot = data["goal_snapshot"]
    goal = snapshot["operation_goal"]
    week = snapshot.get("weekly_goal")
    version = data["goal_ref"]["version"]
    confirmed = snapshot["confirmation"]["status"] == "confirmed_in_conversation"
    title = "小红书月目标＋简单周目标" if week else "小红书运营目标"
    lines = [f"## {title} · 第 {version} 版", "",
        f"{plain(snapshot['company_name'])} · {plain(goal['period']['start'])} 至 {plain(goal['period']['end'])}", "",
        plain(goal["summary"]), "", f"业务方向：{plain(goal['business_direction'])}", "",
        f"目标用户：{'；'.join(map(plain, goal['target_users']))}", "",
        "| 指标 | 过去表现 | 本期目标 |", "| --- | --- | --- |"]
    for metric in goal["metrics"]:
        lines.append(f"| {plain(metric['name'])} | {plain(metric['baseline'])} | {plain(metric['target'])} |")
    lines += ["", "### 怎么统计与检查", ""]
    for metric in goal["metrics"]:
        lines.append(f"- {plain(metric['name'])}：{plain(metric['definition'])}；数据来自{plain(metric['data_source'])}；{plain(metric['review_frequency'])}。")
    for field, title in (("evidence", "目标依据"), ("resources", "人员与资源"),
                         ("constraints", "约束"), ("assumptions", "待验证的判断"), ("risks", "主要风险")):
        if goal[field]:
            lines += ["", f"### {title}", "", *[f"- {plain(item)}" for item in goal[field]]]
    lines += ["", f"预算情况：{plain(goal['budget_statement'])}"]
    if week:
        lines += ["", "### 简单周目标", "",
            f"{plain(week['period']['start'])} 至 {plain(week['period']['end'])}：{plain(week['summary'])}", "",
            f"与月目标的关系：{plain(week['contribution'])}", "",
            "| 指标 | 过去表现 | 本周目标 |", "| --- | --- | --- |"]
        for metric in week["metrics"]:
            lines.append(f"| {plain(metric['name'])} | {plain(metric['baseline'])} | {plain(metric['target'])} |")
        for metric in week["metrics"]:
            lines += ["", f"- {plain(metric['name'])}：{plain(metric['definition'])}；{plain(metric['data_source'])}；{plain(metric['review_frequency'])}。"]
        for field, label in (("evidence", "这样定的依据"), ("resources", "需要配合"),
                             ("constraints", "成立条件"), ("assumptions", "待验证"), ("risks", "注意")):
            if week[field]:
                lines += ["", f"{label}：{'；'.join(map(plain, week[field]))}"]
        lines += ["", f"本周预算：{plain(week['budget_statement'])}", "",
            "本版确认范围包括以上月目标和简单周目标；后续周计划使用这里的周目标。"]
    lines += ["",
        "此版本已确认。目标确认不代表预算审批或付费研究授权。" if confirmed else "此版本待确认，尚未启动研究。"]
    if historical:
        lines += ["", "这是历史目标记录；是否仍可确认以当前任务的最新版本为准。"]
    elif not confirmed:
        lines += ["", f"如无修改，请回复：**确认第 {version} 版目标**。需要修改时直接说明。"]
    return "\n".join(lines)


def goal_brief(data, *, historical=False):
    snapshot = data["goal_snapshot"]
    week = snapshot.get("weekly_goal")
    version = data["goal_ref"]["version"]
    lines = [f"## {'月目标＋简单周目标' if week else '运营目标'} · 第 {version} 版", ""]
    for title, goal in (("月目标" if week else "本期目标", snapshot["operation_goal"]), ("下阶段周目标", week)):
        if not goal:
            continue
        lines += [f"**{title}（{plain(goal['period']['start'])} 至 {plain(goal['period']['end'])}）**", "", excerpt(goal["summary"], 90), ""]
        for metric in goal["metrics"]:
            lines.append(f"- {plain(metric['name'])}：{plain(metric['target'])}")
        if goal.get("contribution"):
            lines += ["", f"对月目标的帮助：{excerpt(goal['contribution'], 90)}"]
        for field, label in (("constraints", "成立条件"), ("risks", "注意")):
            if goal[field]:
                lines += ["", f"{label}（摘要）：{excerpt('；'.join(goal[field]), 90)}"]
        lines += ["", f"预算：{plain(goal['budget_statement'])}", ""]
    lines += ["以上是简版，目标数字完整保留。回复“查看完整目标”可查看统计口径、依据、人员配合、全部条件与风险。", ""]
    if snapshot["confirmation"]["status"] == "confirmed_in_conversation":
        lines += ["此版本已确认。目标确认不代表预算审批或付费研究授权。"]
    elif not historical:
        scope = "月目标和简单周目标" if week else "目标"
        lines += [f"此版本待确认。确认范围包括以上{scope}，不包含付费研究授权。",
            f"如无修改，回复“确认这版目标”或“确认第 {version} 版目标”；需要修改就直接说。"]
    if historical:
        lines += ["这是历史目标记录；是否仍可确认以当前任务的最新版本为准。"]
    return "\n".join(lines)


def prose(value):
    """仅整理段落；保留原文和安全转义，不把存档文字当 Markdown 指令。"""
    paragraphs, current = [], ""
    for line in str(value if value is not None else "未知").splitlines():
        for sentence in re.split(r"(?<=[。！？])", line):
            if current and len(current) + len(sentence) > 180:
                paragraphs.append(plain(current))
                current = ""
            current += sentence
        if current:
            paragraphs.append(plain(current))
            current = ""
    return "\n\n".join(paragraphs)


def excerpt(value, limit=70):
    """旧长字段只摘录完整句/分句并标注，不截断字词，不改写或删减存档。"""
    text = " ".join(str(value).split())
    if len(text) <= limit:
        return plain(text)
    for punctuation in (r"[。！？]", r"[；：，,;:]"):
        # 引号/括号内的问号和逗号属于标题、引文或限定条件，不作为摘录断点。
        pattern = r'"[^"\n]*"|“[^”]*”|「[^」]*」|『[^』]*』|（[^）]*）|\([^)]*\)|' + punctuation
        ends = [match.end() for match in re.finditer(pattern, text)
            if len(match[0]) == 1 and match.end() <= limit]
        if ends:
            return plain(text[:ends[-1]]) + "…（摘录）"
    return "说明较长，请查看完整报告。"


def source_link(source, label=None):
    # 来源 URL 已由保存/读取校验；额外编码 Markdown 定界符。
    return f"[{plain(label or source['source_id'])}]({quote(source['url'], safe=':/?&=%#')})"


def topic_lines(topic, index, visual_analyses=()):
    lines = [f"### 选题 {index}：{plain(topic['title'])}", "",
        f"推荐：{ {'high': '优先考虑', 'medium': '可以考虑', 'low': '暂作观察'}[topic['recommendation']]}", "",
        "**适合企业的原因**", "", prose(topic["why_fit_company"]), "",
        "**目标用户**", "", prose(topic["target_user"]), "", "**依据**", ""]
    for item in topic["evidence"]:
        lines += [("推测：" if item.get("kind") == "inference" else "观察：") + prose(item["fact"]), "", f"来源：{', '.join(map(plain, item['source_refs']))}", ""]
        if item.get("uncertainty"):
            lines += [f"不确定性：{plain(item['uncertainty'])}", ""]
    for receipt in visual_analyses:
        if receipt["analysis_id"] in topic.get("visual_refs", []) and receipt.get("analysis"):
            lines += ["**图片里看到了什么（模型观察，需核对）**", ""]
            total = receipt.get("available_image_count")
            indexes = "、".join(str(i) for i in receipt.get("image_indexes", []))
            lines += [f"已读图号：{indexes}；详情返回{total}张图片。" if total is not None
                else f"已读图号：{indexes}；整篇图片数量未核实。", ""]
            for item in receipt["analysis"]["images"]:
                lines += [f"- 图 {item['image_index']}：{plain(item['visible'])}；局限：{plain(item['uncertainty'])}"]
                if item.get("inference"):
                    lines += [f"  推测：{plain(item['inference'])}"]
            lines += ["", f"可借鉴什么（建议）：{plain(topic.get('visual_borrowing') or '未提出视觉借鉴建议')}", ""]
    if topic.get("planned_photo_count") is not None:
        lines += [f"本选题需要现有照片 {topic['planned_photo_count']} 张；具体画面和可用性仍待核实。", ""]
    for field, title in (("expected_result", "预期结果/待验证假设"), ("required_materials", "所需素材"), ("risks", "风险")):
        lines += [f"**{title}**", "", *[f"- {plain(item)}" for item in topic[field]], ""]
    lines += ["**评分及理由（不代表批准）**", ""]
    for field, title in (("trend", "趋势"), ("demand", "需求"), ("business_fit", "企业匹配"),
                         ("commercial_value", "商业价值"), ("competition", "竞争")):
        rating = topic["ratings"][field]
        lines.append(f"- {title}：{rating['level']}/5，{plain(rating['reason'])}")
    return lines


def research_text(data, *, view="brief", topic_number=None):
    labels = {"success": "已完成并保存", "partial_success": "已保存，但数据不完整", "failed": "研究失败，已保存失败记录"}
    topics, sources = data["candidate_topics"], data["source_references"]
    if topic_number is not None:
        if not 1 <= topic_number <= len(topics):
            raise ValueError(f"本轮共有 {len(topics)} 个选题，请使用对应编号。")
        topic = topics[topic_number - 1]
        refs = {ref for evidence in topic["evidence"] for ref in evidence["source_refs"]}
        return "\n".join([*topic_lines(topic, topic_number, data.get("visual_analyses", [])), "", "### 数据来源", "",
            *[f"- {source_link(source)}" for source in sources if source["source_id"] in refs], "",
            "这是已保存选题的详情，仍待负责人确认；全局限制可回复“查看研究局限”。"])
    if view == "sources":
        return "\n".join([f"## 全部研究来源 · {len(sources)} 条", "",
            *[f"- {source_link(source)} · {plain(source['keyword'])} · {plain(source['collected_at'])}" for source in sources]])
    if view == "limitations":
        return "\n".join(["## 研究局限", "", labels[data["execution_status"]], "",
            *[f"{index}. {plain(item)}" for index, item in enumerate(data["limitations"], 1)]])
    if view == "brief":
        lines = ["## 小红书研究与候选选题", "", labels[data["execution_status"]], "",
            f"本轮 {len(data['queries'])} 个搜索词、{len(sources)} 条来源、{len(topics)} 个候选选题。完整结果已保留。"]
        visuals = data.get("visual_analyses", [])
        seen = [item for item in visuals if item["status"] == "success"]
        lines += ["", f"图片分析：已分析 {len(seen)} 篇、{sum(len(item['image_indexes']) for item in seen)} 张图片。" +
            ("部分图片分析未完成。" if len(seen) < len(visuals) else "") if visuals else "图片分析：本轮未分析图片，不能判断画面内容或效果。"]
        if len(data["research_summary"]) <= 200:
            lines += ["", prose(data["research_summary"])]
        lines += ["", "### 先看研究局限", ""]
        lines += [f"- {excerpt(item)}" for item in data["limitations"][:2]] or ["未记录额外局限，不代表结果已获批准。"]
        if len(data["limitations"]) > 2:
            lines += ["", f"另有 {len(data['limitations']) - 2} 项限制；回复“查看研究局限”读取全部。"]
        if data["execution_status"] == "failed":
            lines += ["", "本轮不能生成周计划。回复“查看完整研究报告”检查失败记录。"]
            return "\n".join(lines)
        lines += ["", "### 关键发现（摘录，非完整结论）", ""]
        by_id = {source["source_id"]: source for source in sources}
        concise_findings = [item for item in data["key_findings"]
            if excerpt(item["statement"]) != "说明较长，请查看完整报告。"]
        for index, item in enumerate(concise_findings[:3], 1):
            refs = [by_id[ref] for ref in item["source_refs"] if ref in by_id]
            link = f" {source_link(refs[0], '来源之一')}" if refs else ""
            lines += [f"**{index}. {'事实' if item['kind'] == 'fact' else '推断'}**：{excerpt(item['statement'])}{link}", "",
                f"注意：{excerpt(item['uncertainty'], 50)}", ""]
        if not concise_findings:
            lines += ["发现的完整表述较长，可回复“查看完整研究报告”阅读；此处不截断结论。"]
        ranked = sorted(enumerate(topics, 1), key=lambda pair: {"high": 0, "medium": 1, "low": 2}[pair[1]["recommendation"]])
        lines += ["", "### 优先查看的选题", "", "按已有推荐等级展示，同级保持原顺序；编号对应完整报告。", ""]
        for index, topic in ranked[:3]:
            lines += [f"**选题 {index}：{plain(topic['title'])}**", "",
                f"匹配原因：{excerpt(topic['why_fit_company'], 80)}", "",
                f"待验证目标：{excerpt('；'.join(topic['expected_result']), 45)}", ""]
        if ranked[3:]:
            lines += ["### 其他候选选题", "", *[f"- 选题 {index}：{plain(topic['title'])}" for index, topic in ranked[3:]], ""]
        lines += ["候选选题仍待确认，尚未制作、发布或投流。", "",
            "详情可回复“查看选题 3 的详细依据”“查看全部研究来源”或“查看完整研究报告”；只读取已有结果，不调用模型或取数接口。"]
        return "\n".join(lines)
    lines = ["## 小红书完整研究报告", "", labels[data["execution_status"]], "", prose(data["research_summary"])]
    for field, title in (("key_findings", "关键发现"), ("user_needs", "用户需求"),
                         ("competitor_findings", "竞品观察"), ("platform_findings", "平台观察")):
        if data[field]:
            lines += ["", f"### {title}", ""]
            for item in data[field]:
                lines += [f"**{'事实' if item['kind'] == 'fact' else '推断'}**", "", prose(item["statement"]), "",
                    f"来源：{', '.join(map(plain, item['source_refs'])) or '无'}", "",
                    f"不确定性：{prose(item['uncertainty'])}", "", f"建议：{prose(item['suggested_action'])}", ""]
    for index, topic in enumerate(topics, 1):
        lines += ["", *topic_lines(topic, index, data.get("visual_analyses", []))]
    for receipt in data.get("visual_analyses", []):
        lines += ["", f"图片分析 {plain(receipt['analysis_id'])}：{plain(receipt['message'])}"]
        if receipt.get("analysis"):
            for item in receipt["analysis"]["images"]:
                lines += [f"- 图 {item['image_index']}，可见：{plain(item['visible'])}；可辨文字：{plain(item['readable_text'])}；推测：{plain(item['inference'])}；局限：{plain(item['uncertainty'])}"]
            lines += [f"- {plain(item)}" for item in receipt["analysis"]["limitations"]]
    lines += ["", "### 数据来源", ""]
    for source in data["source_references"]:
        lines.append(f"- {source_link(source)} · {plain(source['keyword'])} · {plain(source['collected_at'])}")
    lines += ["", "### 研究局限", "", *[f"- {plain(item)}" for item in data["limitations"]], "",
        "候选选题仍待负责人确认；尚未制作、发布或投流。"]
    return "\n".join(lines)


class BusinessTextChat:
    def __init__(self, store, enterprise, workflow, run_plan=None):
        self.store, self.enterprise, self.workflow = store, enterprise, workflow
        self.run_plan = run_plan

    def latest_goal(self, identifier):
        goals = [item for item in self.store.generation_ui_artifacts(identifier) if item.scenario == GOAL_SCENARIO]
        if not goals:
            raise ValueError("当前对话没有已保存的目标，请先制定并保存目标。")
        artifact = goals[-1]
        handoff = OperationGoalStore().load(artifact.data["goal_ref"]["artifact_path"], require_latest=True)
        if handoff["goal_ref"] != artifact.data["goal_ref"]:
            raise ValueError("目标版本已变化，请重新查看最新目标。")
        return artifact, handoff

    def offer(self, identifier, *, restart=False):
        artifact, handoff = self.latest_goal(identifier)
        task = self.enterprise.task(identifier) or {}
        if task.get("phase") in {"goal_input", "goal_running", "goal_failed"}:
            raise ValueError("目标还在补充或修改，请先完成并确认最新目标。")
        if handoff["goal_snapshot"]["confirmation"]["status"] != "confirmed_in_conversation":
            return f"请先回复“确认第 {handoff['goal_ref']['version']} 版目标”。目标确认不会启动付费研究。"
        state = self.store.content_workflow(identifier)
        if state and (state.get("research_draft") or state.get("research_path") and not restart or state["status"] not in {"goal_ready", "failed", "interrupted", "weekly_waiting_research"}):
            raise ValueError("本轮已有结果、可恢复草稿或正在执行，不能重复研究。可回复“检查已有研究结果”。")
        pending = state and any(call["status"] in {"inflight", "pending"} for call in state.get("research_calls", {}).values())
        if pending and (not restart or not state.get("research_query_checked")):
            raise ValueError("原取数任务尚未确认完成，请先回复“查询取数进度”。")
        self.enterprise.update_task(identifier, **{**task, "research_offer": {
            "goal_ref": handoff["goal_ref"], "artifact_id": artifact.artifact_id,
            "revision": state["revision"] if state else 0, "restart": restart}})
        limits = research_limits((state or {}).get("weekly_inputs", {}).get("staff_and_requirements", ""))
        scope = (f"\n计划周期：{state['weekly_inputs']['start']}至{state['weekly_inputs']['end']}。\n研究范围："
            + excerpt((state.get("weekly_choice") or {}).get("research_question", "围绕本周目标寻找图文候选。"), 160)) if state and state.get("weekly_inputs") else ""
        return (f"将使用第 {handoff['goal_ref']['version']} 版目标启动一次小红书研究。" + scope + "\n\n" +
            ("原任务仍未返回；重新研究会创建新任务，可能重复计费。目标与人员安排保留，旧记录也会保留。\n\n" if pending else "") +
            f"本轮最多搜索 {limits['social_search_posts']} 次、读取 {limits['social_get_post_detail']} 篇详情、读取 {limits['social_get_comments']} 篇评论，每篇最多 {limits['comment_limit']} 条；不查询博主账号。达到上限即停止。\n\n" +
            f"筛选后最多分析 {limits['analyze_research_images']} 篇笔记的图片，每篇最多3张；只分析已读取详情的笔记，产生视觉模型费用，失败不自动重试。\n\n" +
            "可能产生 GUAIKEI 数据接口和模型费用，不包含发布或投流；失败后不会自动重新付费取数。\n\n"
            "同意请回复：**同意费用并启动本次研究**。暂不研究可回复“取消研究”。")

    def handle(self, identifier, message, turn_id, run_research):
        text = message.strip().rstrip("。！!")
        weekly = re.fullmatch(r"(制定周计划|生成周计划|制定本周计划|修改周计划|继续周计划|查看周计划)(?:[：:\s]+(.*))?", text, re.S)
        if weekly:
            try:
                state = self.store.content_workflow(identifier)
                if weekly[1] == "查看周计划":
                    if not state or not state.get("weekly_saved"):
                        return "当前对话没有已保存的周计划。请先回复“制定周计划：人员姓名、职责及本周要求”。"
                    prefix = "以下是上次成功保存的版本；本次修改尚未完成。\n\n" if state["status"] != "plan_ready" else ""
                    return prefix + weekly_reply(identifier, {**state, "status": "plan_ready"})
                if weekly[1] == "继续周计划":
                    if not state or not state.get("weekly_inputs"):
                        return "请先回复“制定周计划：人员姓名、职责及本周要求”。"
                    result = self.workflow.act(identifier, WorkflowAction(action="resume_weekly_plan", expected_revision=state["revision"]), run_plan=self.run_plan)
                    return weekly_reply(identifier, result)
                notes = (weekly[2] or "").strip()
                if not notes:
                    task = self.enterprise.task(identifier) or {}
                    self.enterprise.update_task(identifier, **{**task, "weekly_input_pending": True})
                    return "这周有谁能提供什么照片或资料，大概什么时候能交？直接说就行，还没确定也可以告诉我。"
                artifact, goal = self.latest_goal(identifier)
                if goal["goal_snapshot"]["confirmation"]["status"] != "confirmed_in_conversation":
                    return f"请先确认第 {goal['goal_ref']['version']} 版目标，再制定周计划。"
                if not state:
                    state = self.workflow.act(identifier, WorkflowAction(action="bind_goal", expected_revision=0, goal_artifact_id=artifact.artifact_id))
                # 只有开头日期指定计划周；人员说明中的交图日期不覆盖周目标。
                date_match = re.match(r"\d{4}-\d{2}-\d{2}", notes)
                action = "revise_weekly_plan" if weekly[1] == "修改周计划" else "weekly_plan"
                result = self.workflow.act(identifier, WorkflowAction(action=action, expected_revision=state["revision"],
                    plan_notes=notes, week_start=date.fromisoformat(date_match[0]) if date_match and action == "weekly_plan" else None), run_plan=self.run_plan)
                task = self.enterprise.task(identifier) or {}
                self.enterprise.update_task(identifier, **{**task, "weekly_input_pending": False})
                return weekly_reply(identifier, result)
            except (ValueError, OSError, GenerationUiActionConflictError) as exc:
                return "周计划未执行：" + (str(exc) if isinstance(exc, ValueError) else "本地输入或保存状态不可用，请检查后重试。")
        confirm = goal_confirmation(message)
        topic_detail = re.fullmatch(r"查看选题\s*(\d+)\s*(?:的)?(?:详细依据|详情)?", text)
        repair = re.fullmatch(r"修正研究草稿\s+([a-z_0-9.]+)\s+为\s+(.+)", message.strip(), re.S)
        commands = {"确认目标", "启动研究", "开始研究", "重新研究", "同意费用并启动本次研究", "取消研究",
            "检查已有研究结果", "查看研究结果", "查看当前目标", "查看完整目标", "查看研究草稿", "补存研究草稿",
            "查看全部研究来源", "查看研究局限", "查看完整研究报告", "查询取数进度", "继续研究"}
        task = self.enterprise.task(identifier) or {}
        if text in {"好的", "好", "确认", "同意"} and not task and any(
                item.scenario == GOAL_SCENARIO for item in self.store.generation_ui_artifacts(identifier)):
            return "尚未执行确认或取数。请明确回复“确认第 N 版目标”，或回复“启动研究”先核对费用。"
        if text in {"好的", "好", "确认", "同意"} and task.get("phase") in {"goal_review", "goal_ready"}:
            text = "确认目标" if task["phase"] == "goal_review" else "启动研究"
        if confirm is None and not repair and not topic_detail and text not in commands:
            return None
        try:
            if confirm is not None:
                if task.get("phase") in {"goal_input", "goal_running", "goal_failed"}:
                    raise ValueError("目标正在补充或修改，请等待最新草案后再确认。")
                artifact, handoff = self.latest_goal(identifier)
                if confirm and confirm != handoff["goal_ref"]["version"]:
                    raise ValueError(f"当前是第 {handoff['goal_ref']['version']} 版目标，未确认旧版本。")
                if handoff["goal_snapshot"]["confirmation"]["status"] != "confirmed_in_conversation":
                    self.store.confirm_operation_goal(identifier, artifact.artifact_id,
                        idempotency_key=str(uuid4()), user_turn_id=turn_id)
                elif self.store.content_workflow(identifier):
                    return "此版本目标已确认，研究流程已建立；没有再次执行或收费。"
                self.enterprise.update_task(identifier, **{**task, "phase": "goal_ready"})
                scope = "月目标和简单周目标" if handoff["goal_snapshot"].get("weekly_goal") else "目标"
                reply = f"此版本{scope}已确认，尚未启动研究。接下来可以安排周计划：这周有谁能提供什么照片或资料，大概什么时候能交？直接告诉我即可。"
                self.enterprise.update_task(identifier, **{**(self.enterprise.task(identifier) or {}), "weekly_input_pending": True})
                if handoff["goal_snapshot"].get("weekly_goal"):
                    return reply
                return reply + "\n\n也可先单独研究：\n\n" + self.offer(identifier)
            if text in {"查看当前目标", "查看完整目标"}:
                return goal_text(self.latest_goal(identifier)[1], full=text == "查看完整目标")
            if text == "确认目标":
                version = self.latest_goal(identifier)[1]["goal_ref"]["version"]
                return f"尚未执行确认。请检查目标后明确回复：确认第 {version} 版目标。"
            if text in {"启动研究", "开始研究", "重新研究"}:
                return self.offer(identifier, restart=text == "重新研究")
            if text == "取消研究":
                self.enterprise.update_task(identifier, **{**task, "research_offer": None})
                return "已取消待确认的研究启动，不会开始取数。已在执行的任务请使用停止生成。"
            if text == "同意费用并启动本次研究":
                offer = task.get("research_offer")
                if not offer:
                    return "需要先核对本次研究范围。\n\n" + self.offer(identifier)
                _, handoff = self.latest_goal(identifier)
                state = self.store.content_workflow(identifier)
                if handoff["goal_ref"] != offer["goal_ref"] or (state["revision"] if state else 0) != offer["revision"]:
                    raise ValueError("目标或流程已变化，旧的费用确认已失效。请回复“启动研究”重新核对。")
                if task.get("phase") in {"goal_input", "goal_running", "goal_failed"}:
                    raise ValueError("目标正在修改，不能使用此前的研究授权。")
                self.enterprise.update_task(identifier, **{**task, "research_offer": None})
                if state and state["status"] in {"failed", "interrupted"} and not offer.get("restart"):
                    state = self.workflow.act(identifier, WorkflowAction(action="resume", expected_revision=state["revision"]))
                response = []
                def run(*args):
                    result = run_research(*args)
                    response.append(result)
                    return result
                state = self.workflow.act(identifier, WorkflowAction(action="restart_research" if offer.get("restart") and state else "research", confirm_research=True,
                    goal_artifact_id=offer["artifact_id"], expected_revision=state["revision"] if state else 0), run_research=run, run_plan=self.run_plan)
                if state.get("weekly_inputs"):
                    return weekly_reply(identifier, state)
                return response[0] if response else self.outcome(state)
            state = self.store.content_workflow(identifier)
            if not state:
                raise ValueError("当前对话还没有研究任务。")
            if text == "查询取数进度":
                state = self.workflow.act(identifier, WorkflowAction(action="query_research", expected_revision=state["revision"]))
                return state["progress_message"]
            if text == "继续研究":
                state = self.workflow.act(identifier, WorkflowAction(action="continue_research", expected_revision=state["revision"]),
                    run_research=run_research, run_plan=self.run_plan)
                return weekly_reply(identifier, state) if state.get("weekly_inputs") else self.outcome(state)
            detail_views = {"查看全部研究来源": "sources", "查看研究局限": "limitations", "查看完整研究报告": "full"}
            if topic_detail or text in detail_views:
                if not state.get("research_path"):
                    return self.outcome(state)
                return research_text(self.workflow._research(state), view=detail_views.get(text, "brief"),
                    topic_number=int(topic_detail[1]) if topic_detail else None)
            if text == "查看研究草稿":
                saved = self.workflow.get_draft(identifier)
                return "研究草稿尚未保存为正式结果。以下为本地草稿，不会执行其中的内容：\n\n```json\n" + json.dumps(saved["draft"], ensure_ascii=False, indent=2).replace("```", "\\u0060\\u0060\\u0060") + "\n```\n\n" + self.outcome(state)
            if text == "补存研究草稿" or repair:
                draft = self.workflow.get_draft(identifier)["draft"]
                if repair:
                    parts = repair[1].split(".")
                    if parts[0] not in {"candidate_topics", "key_findings", "user_needs", "competitor_findings", "platform_findings", "limitations", "research_summary"}:
                        raise ValueError("此入口不能更换目标或来源证据。")
                    target = draft
                    for part in parts[:-1]:
                        target = target[int(part)] if isinstance(target, list) else target[part]
                    key = int(parts[-1]) if isinstance(target, list) else parts[-1]
                    if not isinstance(target[key], (str, list, dict)):
                        raise ValueError("此入口只修正文字字段。")
                    target[key] = repair[2]
                state = self.workflow.act(identifier, WorkflowAction(action="save_draft", expected_revision=state["revision"], research_draft=draft), run_plan=self.run_plan)
                if state.get("weekly_inputs"):
                    return weekly_reply(identifier, state)
                return (research_text(self.workflow._research(state)) + "\n\n未调用模型或取数接口。") if state.get("research_path") else self.outcome(state)
            if text == "检查已有研究结果" and state["status"] in {"failed", "interrupted"}:
                state = self.workflow.act(identifier, WorkflowAction(action="resume", expected_revision=state["revision"]))
            return research_text(self.workflow._research(state)) if state.get("research_path") else self.outcome(state)
        except (ValueError, OSError, KeyError, IndexError, TypeError, GenerationUiActionConflictError) as exc:
            # 仅回显服务端业务校验，不输出文件内容或第三方错误。
            from tools.content_research import safe_research_draft
            reason = str(exc) if isinstance(exc, (ValueError, GenerationUiActionConflictError)) else "目标或研究文件无法读取，请检查当前版本与保存状态。"
            return "操作未完成：" + safe_research_draft({"message": reason})["message"] + "\n\n未启动新的付费取数。"

    @staticmethod
    def outcome(state):
        if state.get("research_path"):
            return "研究结果已有保存记录，可回复“查看研究结果”。"
        if state.get("research_draft"):
            errors = state.get("validation_errors", [])
            lines = ["研究结果尚未保存，草稿已保留；无需重新取数。"]
            for error in errors:
                lines.append(f"- {plain(error['field'])}：{plain(error['message'])}")
            lines += ["", "可回复“查看研究草稿”检查内容；修正文字可回复：修正研究草稿 字段路径 为 正确文字。也可回复“补存研究草稿”仅重试本地保存。"]
            return "\n".join(lines)
        return (state.get("error") or "本轮尚无已保存研究。") + "\n\n可回复“检查已有研究结果”；确需重跑时回复“重新研究”，重新核对费用。"
