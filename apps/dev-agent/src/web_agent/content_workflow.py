"""单进程本地 Web 的固定运营流程；仅用户请求才能推进或重试。"""

from datetime import date
from uuid import UUID, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, Field
from typing import Literal
from agent.completion import CompletionContract, CompletionDecision

from tools.content_operations import generate_content_weekly_plan
from tools import content_research
from tools.content_research import SAVE_CONTENT_RESEARCH_TOOL_NAME, load_content_research_artifact, safe_research_draft
from tools.content_topic_plan_ui import build_content_topic_plan_ui
from tools.operation_goals import GOAL_SCENARIO, OperationGoalStore
from tools.social_research import SOCIAL_RESEARCH_TOOL_NAMES
from web_agent.weekly_planning import WeeklyPlanning
from web_agent.research_session import LABELS, normalized_request, research_limits, stopped, post_key, visual_receipts, source_receipts
from tools.research_images import IMAGE_TOOL_NAME, analyze_images, image_urls
from tools.research_quality import photo_limit, research_quality_errors
from web_agent.conversations import GenerationUiActionConflictError


class WorkflowAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["bind_goal", "research", "restart_research", "continue_research", "query_research", "plan", "resume", "save_draft", "weekly_plan", "revise_weekly_plan", "resume_weekly_plan"]
    expected_revision: int = Field(ge=0, strict=True)
    goal_artifact_id: UUID | None = None
    week_start: date | None = None
    confirm_research: bool = False
    accept_limitations: bool = False
    research_draft: dict | None = None
    plan_notes: str | None = Field(default=None, max_length=12000)


def public_workflow_state(state):
    if state is None:
        return None
    hidden = {"research_draft", "weekly_draft", "weekly_versions", "weekly_saved", "research_calls", "research_history"}
    return {**{key: value for key, value in state.items() if key not in hidden},
        "has_save_draft": bool(state.get("research_draft")),
        "weekly_saved": {key: value for key, value in (state.get("weekly_saved") or {}).items() if key != "markdown"}}


class ContentWorkflow:
    def __init__(self, store, run_research, run_plan=None):
        self.store = store
        self.run_research = run_research
        self.weekly = WeeklyPlanning(self, run_plan)
        store.interrupt_content_workflows()

    def get(self, conversation_id):
        state = self.store.content_workflow(conversation_id)
        goals = []
        for artifact in self.store.generation_ui_artifacts(conversation_id):
            if artifact.scenario != GOAL_SCENARIO:
                continue
            ref = artifact.data.get("goal_ref", {})
            try:
                handoff = OperationGoalStore().load(ref["artifact_path"], require_latest=True)
                if handoff["goal_ref"] != ref:
                    continue
            except (ValueError, OSError, KeyError):
                continue
            goals.append({"artifact_id": artifact.artifact_id,
                "label": f"{handoff['goal_snapshot']['company_name']} · 第 {ref['version']} 版",
                "confirmed": handoff["goal_snapshot"]["confirmation"]["status"] == "confirmed_in_conversation"})
        if state and state.get("plan_artifact_id"):
            plan = self.store.generation_ui_artifact(conversation_id, state["plan_artifact_id"])
            state = {**state, "review_status": plan.data["approval_status"]}
        return {"workflow": public_workflow_state(state), "goals": goals}

    def get_draft(self, identifier):
        state = self.store.content_workflow(identifier)
        if not state or not state.get("research_draft") or state.get("research_path"):
            raise ValueError("本轮没有可恢复的保存草稿。")
        self._goal(state)
        return {"draft": state["research_draft"], "revision": state["revision"],
            "validation_errors": state.get("validation_errors", [])}

    def _save_research(self, identifier, state, payload, save):
        self._goal(state)
        if state.get("research_path"):
            return state, {"success": False, "message": "本轮研究已保存，不能重复写入。"}
        path = state["goal_ref"]["artifact_path"]
        if payload.get("goal_artifact_path", path) != path:
            return state, {"success": False, "message": "研究必须使用当前流程绑定的目标。"}
        receipts = visual_receipts(state)
        if payload.get("visual_analyses", receipts) != receipts:
            return state, stopped("INVALID_VISUAL_EVIDENCE", "图片分析记录由宿主填入，不能由模型编写或修改。")
        payload = {**payload, "goal_artifact_path": path, "visual_analyses": receipts}
        # 视觉回执也可直接作为来源引用；实际链接由宿主填入，避免模型重复抄写。
        visual_sources = {item["analysis_id"]: {"source_id": item["analysis_id"], "platform": "xiaohongshu",
            "post_id": post_key(item["post_url"]).rsplit("/", 1)[-1], "url": item["post_url"],
            "keyword": "重点笔记图片分析", "collected_at": item["analyzed_at"]}
            for item in receipts if item["status"] == "success"}
        payload["source_references"] = [item for item in payload.get("source_references", [])
            if item.get("source_id") not in visual_sources] + list(visual_sources.values())
        registry = source_receipts(state)
        unknown = [item.get("source_id", "") for item in payload["source_references"] if post_key(item.get("url", "")) not in registry]
        if unknown:
            state = self._save(identifier, state, research_draft=safe_research_draft(payload),
                validation_errors=[{"field": "source_references", "message": "来源不属于本轮成功取数回执：" + "、".join(unknown)}])
            return state, stopped("UNBOUND_RESEARCH_SOURCE", "来源必须来自本轮成功取数；搜索链接不能冒充已读取正文。")
        payload["source_references"] = [{**item, **registry[post_key(item["url"])]} for item in payload["source_references"]]
        if any(item["verification"] == "search_only" for item in payload["source_references"]):
            payload["limitations"] = list(dict.fromkeys([*payload.get("limitations", []), "部分来源仅搜索到，尚未读取正文，不能视作事实已核实。"] ))
            if payload.get("execution_status") == "success":
                payload["execution_status"] = "partial_success"
        source_posts = {item.get("source_id"): post_key(item.get("url", "")) for item in payload["source_references"]}
        topics = []
        for topic in payload.get("candidate_topics", []):
            if topic.get("visual_borrowing") and not topic.get("visual_refs"):
                referenced = {source_posts.get(ref) for evidence in topic.get("evidence", []) for ref in evidence.get("source_refs", [])}
                topic = {**topic, "visual_refs": [item["analysis_id"] for item in receipts
                    if item["status"] == "success" and post_key(item["post_url"]) in referenced]}
            topics.append(topic)
        payload["candidate_topics"] = topics
        if any(item["status"] == "failed" for item in receipts):
            payload["limitations"] = list(dict.fromkeys([*payload.get("limitations", []), "部分图片未完成分析，不能作为已看图依据。"] ))
            if payload.get("execution_status") == "success":
                payload["execution_status"] = "partial_success"
        # 先保存未验证草稿，再执行正式校验；不把它当成可进入周计划的研究工件。
        state = self._save(identifier, state, research_draft=safe_research_draft(payload))
        quality_errors = research_quality_errors(payload, limit=photo_limit(
            (state.get("weekly_inputs") or {}).get("staff_and_requirements", "")))
        if quality_errors:
            message = quality_errors[0]["message"]
            state = self._save(identifier, state, validation_errors=quality_errors, save_error=message)
            return state, {"success": False, "validation_errors": quality_errors,
                "message": message, "retry_scope": "local_save_only"}
        details = sum(call["tool"] == "social_get_post_detail" and call["status"] == "success"
            for call in state.get("research_calls", {}).values())
        import re
        if state.get("research_calls") and details <= 2 and re.search(r"可确认的规律|普遍规律|几乎都靠.{0,50}取胜", str(payload.get("research_summary", ""))):
            message = "本轮正文样本不超过两篇，摘要应写样本信号和待验证假设，不能断言普遍规律或成功原因。"
            errors = [{"field": "research_summary", "message": message}]
            state = self._save(identifier, state, validation_errors=errors, save_error=message)
            return state, {"success": False, "validation_errors": errors, "message": message}
        # 对明确的结论偷换做窄校验；语义是否充分仍需业务复核。
        evidence = [(f"candidate_topics.{i}.evidence.{j}", item) for i, topic in enumerate(payload.get("candidate_topics", []))
            for j, item in enumerate(topic.get("evidence", []))]
        for field, item in evidence:
            if item.get("kind", "fact") == "fact" and re.search(r"说明.{0,80}(?:更能|更有效)|(?:互动最高|高互动).{0,50}依靠|证明.{0,50}(?:带来|促成|导致)", item.get("fact", "")):
                message = "选题依据把观察推成了效果结论；请只保留可核实的观察，把可能的解释另列为带不确定性的推测，不能认定因果。"
                errors = [{"field": field, "message": message}]
                state = self._save(identifier, state, validation_errors=errors, save_error=message)
                return state, {"success": False, "validation_errors": errors, "message": message}
        try:
            result = save(**payload)
            if result.get("success"):
                research = self._research({**state, "research_path": result["artifact_path"]})
        except Exception:
            result = {"success": False, "error_type": "CONTENT_RESEARCH_WRITE_FAILED",
                "message": "本地保存或工件核验未完成，草稿已保留。"}
        if result.get("success"):
            state = self._save(identifier, state, research_path=result["artifact_path"], research_draft=None,
                research_status=research["execution_status"], limitations=research["limitations"],
                validation_errors=[], save_error=None)
        else:
            safe_error = safe_research_draft({"message": result.get("message", "研究结果校验失败。"),
                "validation_errors": result.get("validation_errors", [])})
            state = self._save(identifier, state, validation_errors=safe_error["validation_errors"], save_error=safe_error["message"],
                progress_message="正在修正研究报告的保存格式，已取得的数据仍然保留。")
        return state, result

    def _research_finished(self, identifier, state):
        if not state.get("research_path"):
            has_draft = bool(state.get("research_draft"))
            return self._save(identifier, state, status="failed",
                error_code="RESEARCH_SAVE_FAILED" if has_draft else "RESEARCH_NOT_SAVED",
                error=("研究结果未保存，不能生成周计划。草稿已保留，可在对话中修正后本地补存，无需重新取数。"
                    if has_draft else "研究对话已结束，但没有形成有效研究文件或保存草稿，无法直接补存。"))
        research = self._research(state)
        if research["execution_status"] == "failed":
            return self._save(identifier, state, status="failed", error_code="RESEARCH_FAILED",
                error="已保存的研究被标记为失败，不能生成周计划。请检查研究局限或重新建立流程。")
        return self._save(identifier, state, status="research_ready", stage="plan", error=None, error_code=None)

    def _save(self, identifier, state, **changes):
        return self.store.save_content_workflow(identifier, {**state, **changes},
            expected_revision=state.get("revision", 0))

    def _goal(self, state):
        handoff = OperationGoalStore().load(state["goal_ref"]["artifact_path"],
            require_confirmed=True, require_latest=True)
        if handoff["goal_ref"] != state["goal_ref"]:
            raise ValueError("目标版本不匹配。")
        return handoff

    def _research(self, state):
        research = load_content_research_artifact(state["research_path"])
        if research["goal_ref"] != state["goal_ref"]:
            raise ValueError("研究目标与当前流程不一致。")
        return research

    def act(self, identifier, request, *, run_research=None, run_plan=None):
        weekly = WeeklyPlanning(self, run_plan) if run_plan else self.weekly
        state = self.store.content_workflow(identifier)
        if request.expected_revision != (state["revision"] if state else 0):
            raise GenerationUiActionConflictError("流程已变化，请刷新后再操作。")
        if request.action == "weekly_plan" and state is None and request.goal_artifact_id is not None:
            state = self.act(identifier, WorkflowAction(action="bind_goal", expected_revision=0,
                goal_artifact_id=request.goal_artifact_id))
        if request.action == "research" and state is None and request.goal_artifact_id is not None:
            if not request.confirm_research:
                raise ValueError("必须明确确认启动研究。")
            state = self.act(identifier, WorkflowAction(action="bind_goal", expected_revision=0,
                goal_artifact_id=request.goal_artifact_id))
        if request.action == "bind_goal":
            if state is not None or request.goal_artifact_id is None:
                raise ValueError("每个对话只运行一个流程；更换目标请新建对话。")
            artifact = self.store.generation_ui_artifact(identifier, str(request.goal_artifact_id))
            if artifact.scenario != GOAL_SCENARIO:
                raise ValueError("请选择当前对话中的已确认目标卡片。")
            state = {"workflow_id": str(uuid4()), "goal_ref": artifact.data["goal_ref"],
                "status": "goal_ready", "stage": "research", "error": None,
                "research_path": None, "plan_artifact_id": None, "attempts": 0}
            self._goal(state)
            return self._save(identifier, state)
        if state is None or state["status"] in {"research_running", "plan_running", "save_running"}:
            raise ValueError("请先绑定目标，或等待当前阶段结束。")
        if request.action == "query_research":
            from dataclasses import replace
            from tools.guaikei import GuaikeiCli
            from tools.social_research import SocialResearchTools
            self._goal(state)
            handlers = SocialResearchTools(replace(GuaikeiCli.from_env(), query_only=True)).handlers()
            calls = dict(state.get("research_calls", {}))
            for key, call in calls.items():
                if call["status"] in {"inflight", "pending"}:
                    if call["tool"] == IMAGE_TOOL_NAME:
                        calls[key] = {**call, "status": "failed", "result": stopped("VISUAL_INTERRUPTED", "图片分析中断，不能查询或自动重新调用模型。")}
                        continue
                    result = handlers[call["tool"]](**call["params"])
                    calls[key] = {**call, "status": "success" if result.get("success") else "pending", "result": result}
            pending = any(call["status"] in {"inflight", "pending"} for call in calls.values())
            return self._save(identifier, state, research_calls=calls, research_query_checked=True,
                research_fetch_stop="PROVIDER_RESULT_PENDING" if pending else None,
                progress_message=("原取数任务仍未返回，可稍后再查询；没有创建新任务。" if pending else
                    "已取回原任务结果，可回复“继续研究”整理；不会创建新取数任务。" if calls else
                    "这次旧记录没有保存原请求参数，无法自动查询。目标与人员安排仍保留；需重新取数时回复“重新研究”核对费用。"))
        if request.action in {"restart_research", "continue_research"}:
            if state["status"] not in {"failed", "interrupted", "goal_ready"}:
                raise ValueError("只有未完成的研究可以恢复；已完成结果请直接复用。")
            if request.action == "restart_research" and not request.confirm_research:
                raise ValueError("重新研究需要重新核对费用并明确确认。")
            if request.action == "continue_research" and not state.get("research_calls"):
                raise ValueError("没有可复用的取数记录，请先检查已有研究结果。")
            if any(call["status"] in {"inflight", "pending"} for call in state.get("research_calls", {}).values()) and (
                    request.action == "continue_research" or not state.get("research_query_checked")):
                raise ValueError("尚有原任务状态未确认，请先回复“查询取数进度”，不要重复创建。")
            history = state.get("research_history", [])
            state = self._save(identifier, state, research_history=[*history, {
                "research_path": state.get("research_path"), "research_draft": state.get("research_draft"),
                "research_calls": state.get("research_calls", {})}], research_path=None, research_draft=None,
                research_calls={} if request.action == "restart_research" else state["research_calls"],
                research_fetch_stop=None, research_query_checked=False,
                research_cache_only=request.action == "continue_research", status="goal_ready", error_code=None)
            request = request.model_copy(update={"action": "research", "confirm_research": True})
        if request.action in {"weekly_plan", "revise_weekly_plan"}:
            return weekly.start(identifier, state, request)
        if request.action == "resume_weekly_plan":
            if not state.get("weekly_inputs"):
                raise ValueError("请先提供人员职责并启动周计划。")
            if state["status"] == "plan_ready":
                return state
            return weekly.resume(identifier, state)
        if request.action == "resume":
            if state["status"] not in {"failed", "interrupted"}:
                raise ValueError("当前阶段不需要恢复。")
            # 周计划卡片写入后进程中断时，也不重复生成卡片。
            plans = [x for x in self.store.generation_ui_artifacts(identifier)
                if x.data.get("workflow_id") == state["workflow_id"]]
            if plans:
                return self._save(identifier, state, status="plan_review", stage="review",
                    plan_artifact_id=plans[-1].artifact_id, error=None)
            if state.get("research_path"):
                result = self._research(state)
                return self._save(identifier, state, status="research_ready" if result["execution_status"] != "failed" else "failed",
                    stage="plan", error=None if result["execution_status"] != "failed" else "研究未形成可用选题，目标和人员安排已保留。先查询取数进度，再决定继续整理或重新研究。")
            if state.get("research_draft"):
                return self._research_finished(identifier, state)
            return self._save(identifier, state, status="goal_ready", stage="research", error=None)
        if request.action == "save_draft":
            if state["status"] not in {"failed", "interrupted"} or not state.get("research_draft") or state.get("research_path"):
                raise ValueError("当前没有可补存的草稿。")
            self._goal(state)
            draft = safe_research_draft(request.research_draft if request.research_draft is not None else state["research_draft"])
            if draft.get("goal_artifact_path") != state["goal_ref"]["artifact_path"]:
                raise ValueError("补存不能更换目标。")
            state = self._save(identifier, state, status="save_running", error=None)
            try:
                saver = content_research.ContentResearchTools(output_dir=content_research.CONTENT_RESEARCH_OUTPUT_DIR)
                state, _ = self._save_research(identifier, state, draft, saver.save_result)
                state = self._research_finished(identifier, state)
            except (OSError, ValueError):
                state = self.store.content_workflow(identifier)
                return self._save(identifier, state, status="failed", error_code="RESEARCH_SAVE_FAILED",
                    error="本地补存未完成，草稿仍保留。请检查目标版本、字段和本地文件权限；未重新取数。")
            if state.get("research_path"):
                self.store.append_assistant_response(identifier, "研究结果已通过本地校验并补存。未调用模型或取数接口；后续请按当前研究状态继续。",
                    trace=["固定运营流程：本地草稿补存完成，未调用模型或取数接口。"], duration_ms=0)
            if state.get("weekly_inputs") and state["status"] == "research_ready":
                return weekly.run(identifier, state, prepare=False)
            return state
        if request.action == "research":
            if state["status"] not in {"goal_ready", "weekly_waiting_research"} or state.get("research_path"):
                raise ValueError("请先恢复流程；已保存的研究不会重复取数。")
            if not request.confirm_research:
                raise ValueError("必须明确确认启动研究；真实取数可能计费。")
            self._goal(state)
            state = self._save(identifier, state, status="research_running", stage="research",
                attempts=state["attempts"] + 1, error=None,
                research_limits=research_limits(state.get("weekly_inputs", {}).get("staff_and_requirements", "")),
                progress_message="正在整理研究问题；本轮取数有固定上限。")

            def configure(agent):
                handlers = agent.tool_handlers
                save = handlers[SAVE_CONTENT_RESEARCH_TOOL_NAME]
                def inspect_images(url, image_indexes):
                    calls = state.get("research_calls", {})
                    # scoped已经登记当前inflight；一个笔记不因换图片组合而再次计费。
                    if sum(call["tool"] == IMAGE_TOOL_NAME and post_key(call["params"]["url"]) == post_key(url) for call in calls.values()) > 1:
                        return stopped("VISUAL_ALREADY_REQUESTED", "该笔记已分析或尝试分析，请复用原记录，不换图片组合重试。")
                    for call in calls.values():
                        if call["tool"] != "social_get_post_detail" or call["status"] != "success":
                            continue
                        for post in call.get("result", {}).get("items", []):
                            if post_key(url) not in {post_key(call["params"]["url"]), post_key(post.get("url", ""))}:
                                continue
                            try:
                                image_urls(post, image_indexes)
                            except ValueError as exc:
                                return stopped("INVALID_RESEARCH_IMAGES", str(exc))
                            result = analyze_images(post, image_indexes)
                            _, key = normalized_request(IMAGE_TOOL_NAME, (), {"url": url, "image_indexes": image_indexes})
                            return {**result, "analysis_id": "visual-" + key[:12], "post_url": url,
                                "available_image_count": len(post.get("image_urls") or ([post["cover_url"]] if post.get("cover_url") else [])),
                                "image_indexes": image_indexes, "trust_level": "untrusted_external_data"}
                    return stopped("DETAIL_REQUIRED", "先读取本轮重点笔记详情；不能分析未绑定笔记或任意图片。")
                handlers[IMAGE_TOOL_NAME] = inspect_images
                # 用执行层绑定目标，不能只依赖提示词；每次取数前重查目标版本。
                def scoped(name, handler):
                    def wrapped(*args, **kwargs):
                        nonlocal state
                        self._goal(state)
                        try:
                            params, key = normalized_request(name, args, kwargs)
                        except ValueError as exc:
                            return stopped("INVALID_RESEARCH_REQUEST", str(exc))
                        calls = dict(state.get("research_calls", {}))
                        if key in calls:
                            return calls[key].get("result") or stopped("RESEARCH_FETCH_STOPPED", "原请求状态未确认，请查询取数进度。")
                        if state.get("research_fetch_stop") or any(call["status"] in {"inflight", "pending"} for call in calls.values()):
                            return {"success": False, "error_type": "RESEARCH_FETCH_STOPPED", "retryable": False,
                                "message": "本轮已有取数任务未确认完成，已停止后续取数。请保留现有证据和局限并保存研究，不要换关键词或接口重试。"}
                        if state.get("research_cache_only"):
                            return stopped("RESEARCH_CACHE_ONLY", "本次只整理已有证据，缺少的资料请写为局限，不创建新任务。")
                        count = sum(call["tool"] == name for call in calls.values())
                        limit = state["research_limits"][name]
                        if count >= limit or (name == "social_get_comments" and params["limit"] > state["research_limits"]["comment_limit"]):
                            return stopped("RESEARCH_BUDGET_EXCEEDED", "已达到本轮授权的取数上限，请使用已有证据并说明局限。")
                        calls[key] = {"tool": name, "params": params, "status": "inflight"}
                        state = self._save(identifier, state, research_calls=calls,
                            progress_message=f"正在{LABELS.get(name, '取数')}，本轮第 {count + 1}/{limit} 次。")
                        result = handler(**params)
                        pending = result.get("error_type") in {"PROVIDER_TIMEOUT", "PROVIDER_RESULT_PENDING"}
                        calls[key] = {**calls[key], "status": "pending" if pending else "success" if result.get("success") else "failed", "result": result}
                        state = self._save(identifier, state, research_calls=calls,
                            progress_message="正在根据已返回的样本整理候选选题；不会扩大取数范围。")
                        if result.get("error_type") in {"PROVIDER_TIMEOUT", "PROVIDER_RESULT_PENDING"}:
                            state = self._save(identifier, state, research_fetch_stop=result["error_type"])
                            return {**result, "retryable": False}
                        return result
                    return wrapped
                for name in (*SOCIAL_RESEARCH_TOOL_NAMES, IMAGE_TOOL_NAME):
                    if name in handlers:
                        handlers[name] = scoped(name, handlers[name])

                def save_checkpoint(**payload):
                    nonlocal state
                    state = self._save(identifier, state, progress_message="正在检查研究依据并保存；格式修正不会重新取数。")
                    state, result = self._save_research(identifier, state, payload, save)
                    return result
                handlers[SAVE_CONTENT_RESEARCH_TOOL_NAME] = save_checkpoint
                def verify_research():
                    current = self.store.content_workflow(identifier)
                    research = self._research(current) if current.get("research_path") else None
                    pending = any(call["status"] in {"inflight", "pending"} for call in current.get("research_calls", {}).values())
                    ok = bool(research and research["execution_status"] == "success" and source_receipts(current) and not pending)
                    return CompletionDecision(status="completed" if ok else "needs_review",
                        reason="RESEARCH_PERSISTED_AND_LINKED" if ok else "RESEARCH_PARTIAL_OR_UNVERIFIED",
                        evidence={"workflow_id": current["workflow_id"], "goal_ref": current["goal_ref"],
                            "research_status": research["execution_status"], "pending_requests": pending,
                            "source_count": len(source_receipts(current))} if research else {})
                agent.completion_contract = CompletionContract("research", verify_research)

            try:
                (run_research or self.run_research)(identifier, state["goal_ref"]["artifact_path"], configure)
                state = self._research_finished(identifier, state)
                if state.get("weekly_inputs") and state["status"] == "research_ready":
                    return weekly.run(identifier, state, prepare=False)
                if state.get("error_code") == "RESEARCH_SAVE_FAILED":
                    self.store.append_assistant_response(identifier, "系统校验结果：本轮研究尚未保存，不能生成周计划。可回复“查看研究草稿”修正并补存，无需重新研究。",
                        trace=["固定运营流程：保存失败，已保留本地草稿及校验错误。"], duration_ms=0)
                return state
            except Exception as exc:
                state = self.store.content_workflow(identifier)
                # 不回显 Provider/模型异常，可能含凭据；已保存的结果仍可恢复。
                detail = getattr(exc, "detail", {})
                code = detail.get("error_type") if isinstance(detail, dict) else None
                reasons = {"MODEL_CONNECTION_ERROR": "模型连接失败。", "SKILL_ACTIVATION_ERROR": "研究 Skill 未能激活。",
                    "ATTACHMENTS_PROCESSING": "当前对话附件仍在解析。", "AGENT_EXECUTION_ERROR": "Agent 执行中断。"}
                return self._save(identifier, state, status="failed", error_code=code if code in reasons else "RESEARCH_INTERRUPTED",
                    error=reasons.get(code, "研究执行或结果校验中断。") +
                    "可先恢复已保存结果；没有结果时，重新启动可能再次计费。")
        if request.action == "plan":
            if state["status"] != "research_ready" or request.week_start is None or request.week_start.weekday() != 0:
                raise ValueError("需要已保存的研究结果和一个周一日期。")
            research = self._research(state)
            if (research["execution_status"] == "partial_success" or research["limitations"]) and not request.accept_limitations:
                raise ValueError("请先阅读并确认研究局限，再生成周计划。")
            state = self._save(identifier, state, status="plan_running", stage="plan", error=None)
            try:
                plan = generate_content_weekly_plan(request.week_start.isoformat(), research_artifact_path=state["research_path"])
                plan["workflow_id"] = state["workflow_id"]
                ui = build_content_topic_plan_ui(plan)
                ui["artifact_id"] = str(uuid5(UUID(state["workflow_id"]), "topic-plan"))
                self.store.append_assistant_response(identifier, "周计划已生成，请逐项审核选题。", ui_artifacts=[ui],
                    trace=["固定运营流程：复用已保存研究生成周计划，未调用模型或取数接口。"], duration_ms=0)
                return self._save(identifier, state, status="plan_review", stage="review", plan_artifact_id=ui["artifact_id"])
            except Exception:
                return self._save(identifier, state, status="failed",
                    error="周计划未正常保存。恢复会复用已有研究，不重复取数。")
        raise ValueError("不支持该流程操作。")
