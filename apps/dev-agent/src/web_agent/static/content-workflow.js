// 对话承载业务结果和操作；侧栏只反映服务端阶段。
export const isBusinessArtifact = (artifact) => ["operation_goal_confirmation", "content_topic_plan"].includes(artifact.scenario);
export const reviewDone = (payload) => payload?.phase === "plan_review"
  && ["confirmed_in_conversation", "approved", "discarded"].includes(payload.workflow?.review_status);
export function canOperateArtifact(artifact, payload) {
  if (!payload || artifact.artifact_id !== payload.artifact?.artifact_id) return false;
  return artifact.scenario === "content_topic_plan" && payload.phase === "plan_review" && !reviewDone(payload);
}
export function artifactForTask(artifact, payload, busy = false) {
  const latest = artifact.artifact_id === payload?.artifact?.artifact_id ? payload.artifact : artifact;
  if (!busy && canOperateArtifact(latest, payload)) return latest;
  return { ...latest, spec: { ...latest.spec, interaction_mode: "read_only", actions: [] } };
}
export function taskSteps(payload) {
  const phase = payload?.phase || "idle", failed = ["failed", "interrupted", "goal_failed"].includes(phase);
  if (payload?.workflow?.weekly_inputs) {
    const state = payload.workflow;
    const index = state.weekly_phase === "prepare" && (phase === "plan_running" || failed && state.stage === "plan") ? 2
      : ["weekly_waiting_research", "research_running", "save_running"].includes(phase) ? 3
        : failed && state.stage === "research" ? 3 : 4;
    return ["目标制定", "目标确认", "周计划准备", "按需研究", "计划交付"].map((name, i) => ({ name,
      status: phase === "plan_ready" || i < index ? "已完成" : i > index ? "未开始"
        : failed ? "未完成" : phase.endsWith("running") ? "进行中" : "等待你处理" }));
  }
  const index = { idle: -1, goal_input: 0, goal_running: 0, goal_failed: 0, goal_review: 1,
    goal_ready: 2, research_running: 2, research_ready: 3, plan_running: 3, plan_review: 3 }[phase]
    ?? (["plan", "review"].includes(payload?.workflow?.stage) ? 3 : 2);
  return ["目标制定", "目标确认", "研究选题", "周计划交付"].map((name, i) => ({ name,
    status: reviewDone(payload) || i < index ? "已完成" : i > index || index < 0 ? "未开始"
      : failed ? "未完成" : phase.endsWith("running") ? "进行中" : "等待你处理" }));
}

export function mountContentWorkflow(root, { messages, onState, setBusy, sendChat, reloadConversation }) {
  let identifier = null, busy = false, payload = null, timer, epoch = 0;
  let rendered = "";
  const progress = root.querySelector(".task-progress-body");
  const panel = document.createElement("article");
  panel.className = "workflow-chat-message";
  panel.setAttribute("aria-label", "周计划操作");
  const heading = document.createElement("h3"), notice = document.createElement("p"), controls = document.createElement("fieldset");
  notice.setAttribute("role", "status"); controls.className = "workflow-controls";
  panel.append(heading, notice, controls);
  const labels = { idle: "尚未开始运营任务", goal_input: "等待补充目标信息", goal_failed: "目标尚未完成",
    goal_running: "正在制定目标", goal_review: "等待聊天确认目标", goal_ready: "目标已确认，可以准备周计划",
    research_running: "正在研究小红书", save_running: "正在本地补存研究（不取数）", research_ready: "研究已保存，可以生成周计划", plan_running: "正在生成周计划",
    weekly_waiting_research: "周计划需要研究，等待聊天授权费用", plan_ready: "周计划已保存，可下载或修改",
    plan_review: "历史周计划审核", failed: "本次执行未完成", interrupted: "上次执行中断" };
  function node(tag, text, parent = controls) {
    const element = document.createElement(tag); element.textContent = text; parent.append(element); return element;
  }
  function button(label, callback) {
    const element = node("button", label); element.type = "button";
    element.addEventListener("click", callback); return element;
  }
  function render() {
    const phase = payload?.phase || "idle", state = payload?.workflow;
    const phaseLabel = state?.error_code === "RESEARCH_SAVE_FAILED" ? "研究结果未保存，需要修正" : labels[phase];
    progress.replaceChildren();
    node("h3", identifier && phase !== "idle" ? "小红书运营" : "暂无运营任务", progress);
    node("p", reviewDone(payload) ? "本轮审核已完成" : phaseLabel || "等待状态更新", progress);
    if (state?.progress_message && phase.endsWith("running")) node("p", state.progress_message, progress);
    const steps = node("ol", "", progress);
    for (const step of taskSteps(payload)) {
      const row = node("li", "", steps); row.dataset.status = step.status;
      node("span", step.name, row); node("small", step.status, row);
    }
    node("p", "详细内容与操作均在对话中。", progress).className = "task-progress-hint";
    onState(payload, busy);
    // 目标和收费研究使用聊天确认；新周计划直接交付，旧卡片仍可回查。
    if (!identifier || !payload || !["research_ready", "plan_review", "plan_ready"].includes(phase)) { panel.remove(); return; }
    const nearBottom = messages.scrollHeight - messages.scrollTop - messages.clientHeight < 100;
    messages.append(panel);
    controls.disabled = busy;
    const key = JSON.stringify([phase, state, payload.artifact?.artifact_id, payload.research?.error]);
    if (key === rendered) return;
    rendered = key; controls.replaceChildren();
    heading.textContent = reviewDone(payload) ? "本轮选题审核已完成" : phaseLabel;
    if (phase === "research_ready" && !payload.research?.error) {
      const week = payload.artifact?.data?.goal_snapshot?.weekly_goal;
      const dateLabel = node("label", week ? "已确认的周目标开始日期" : "周计划开始日期（周一）");
      const date = node("input", "", dateLabel); date.type = "date";
      if (week) { date.value = week.period.start; date.readOnly = true; }
      const staffLabel = node("label", "人员姓名、职责与本周要求");
      const staff = node("textarea", "", staffLabel); staff.rows = 4; staff.maxLength = 12000;
      staff.placeholder = "例如：王敏负责案例照片，李强负责现场细节图；本周优先小户型。人员未知可明确写暂无名单。";
      node("p", "将检查已有研究是否适用，并保留研究局限。新周计划生成后直接交付，不启动制作或发布。");
      button("生成周计划", () => {
        if (!date.value || !staff.value.trim()) { notice.textContent = "请选择周一日期，并填写人员职责与本周要求。"; return; }
        void sendChat(`制定周计划：${date.value}；${staff.value.trim()}`);
      });
    } else if (phase === "plan_ready") {
      const link = node("a", "下载周运营计划");
      link.href = `/api/conversations/${identifier}/weekly-plan?version=${state.weekly_saved.version}`;
      const label = node("label", "修改要求（保留已有研究）");
      const revision = node("textarea", "", label); revision.rows = 3; revision.maxLength = 12000;
      button("修改周计划", () => {
        if (!revision.value.trim()) { notice.textContent = "请填写具体修改要求。"; return; }
        void sendChat(`修改周计划：${revision.value.trim()}`);
      });
      const topicLabel = node("label", "选择一条制作小红书图文");
      const topic = node("select", "", topicLabel);
      for (const [title, contentId] of Object.entries(state.weekly_saved.content_ids || {})) {
        const option = node("option", title, topic); option.value = contentId;
      }
      const noteLabel = node("label", "本条制作要求（已有研究会复用）");
      const note = node("textarea", "", noteLabel); note.maxLength = 4000;
      button("生成此条图文候选", async () => {
        if (!topic.value || busy) return;
        busy = true; controls.disabled = true; setBusy(true); notice.textContent = "正在制作此条图文，不重新取数…";
        try {
          const path = `/api/conversations/${identifier}/content-production`;
          const current = await fetch(path); const currentData = await current.json();
          if (!current.ok) throw new Error(currentData.detail?.message || "内容状态读取失败");
          const prior = currentData.drafts.find(item => item.content_id === topic.value);
          const response = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ plan_version: state.weekly_saved.version, content_id: topic.value,
              expected_version: prior?.version || 0, notes: note.value.trim(), idempotency_key: crypto.randomUUID() }) });
          const result = await response.json();
          if (!response.ok) throw new Error(result.detail?.message || "图文尚未生成，请检查来源和模型状态。");
          await reloadConversation?.(); notice.textContent = "图文候选已保存，请在新卡片核对和审核。";
        } catch (error) { notice.textContent = error.message; }
        finally { busy = false; controls.disabled = false; setBusy(false); }
      });
    } else if (phase === "plan_review") {
      node("p", reviewDone(payload) ? "审核结果已保存。尚未制作、发布或投流。" : "请在上方周计划卡片逐项审核；要求修改时请填写具体说明。");
    } else node("p", phase.endsWith("running") ? "正在执行；刷新页面不会重新启动任务。" : "请检查上方研究结果。");
    if (nearBottom) messages.scrollTop = messages.scrollHeight;
  }
  async function refresh() {
    clearTimeout(timer); const requestedId = identifier, requestEpoch = ++epoch;
    try {
      const response = await fetch(requestedId ? `/api/conversations/${requestedId}/current-task` : "/api/enterprise");
      if (!response.ok) throw new Error("任务状态读取失败，请刷新页面重试。");
      const data = await response.json();
      if (requestEpoch !== epoch || requestedId !== identifier) return false;
      if (!payload) notice.textContent = "";
      payload = requestedId ? data : { phase: "idle", profile: data.profile };
      const running = ["research_running", "plan_running", "goal_running", "save_running"].includes(payload.phase);
      setBusy(running); render();
      if (requestedId && (running || busy)) timer = setTimeout(refresh, 3000);
      return true;
    } catch (error) {
      if (requestEpoch !== epoch || requestedId !== identifier) return false;
      payload = null; rendered = ""; onState(null, true);
      progress.replaceChildren(); node("p", error.message, progress);
      panel.remove();
      return false;
    }
  }
  return {
    refresh,
    get payload() { return payload; },
    update(nextId, nextBusy) {
      const changed = nextId !== identifier, busyChanged = busy !== nextBusy;
      identifier = nextId; busy = nextBusy;
      if (changed) {
        epoch++; clearTimeout(timer); payload = null; rendered = "";
        panel.remove(); notice.textContent = ""; render(); void refresh();
      } else if (busyChanged) {
        render();
        if (busy) { clearTimeout(timer); timer = setTimeout(refresh, 300); }
      }
    },
  };
}
