import { test } from "node:test";
import assert from "node:assert/strict";
import { artifactForTask, canOperateArtifact, taskSteps, reviewDone, mountContentWorkflow } from "../../src/web_agent/static/content-workflow.js";

const goal = { artifact_id: "goal-v1", scenario: "operation_goal_confirmation", data: { summary: "完整目标" },
  spec: { interaction_mode: "host_action", actions: [{ id: "confirm_operation_goal" }] } };
const plan = { ...goal, artifact_id: "plan-v1", scenario: "content_topic_plan" };
test("only the selected artifact in its actionable phase can expose actions", () => {
  const current = { phase: "goal_review", artifact: goal };
  assert.equal(canOperateArtifact(goal, current), false);
  for (const phase of ["goal_input", "goal_running", "goal_failed", "goal_ready", "research_running"]) {
    assert.equal(canOperateArtifact(goal, { ...current, phase }), false);
  }
  assert.equal(canOperateArtifact({ ...goal, artifact_id: "old-goal" }, current), false);
  assert.equal(canOperateArtifact(plan, current), false);
  assert.equal(canOperateArtifact(goal, null), false);
});
test("history keeps its details but is read-only; current data uses the server snapshot", () => {
  const next = { ...goal, data: { summary: "更新后的目标" } };
  assert.equal(artifactForTask(goal, { phase: "goal_review", artifact: next }).data.summary, "更新后的目标");
  for (const payload of [null, { phase: "goal_ready", artifact: goal }]) {
    const result = artifactForTask(goal, payload);
    assert.equal(result.data.summary, "完整目标");
    assert.equal(result.spec.interaction_mode, "read_only");
    assert.deepEqual(result.spec.actions, []);
  }
  assert.deepEqual(artifactForTask(goal, { phase: "goal_review", artifact: goal }, true).spec.actions, []);
  assert.equal(goal.spec.actions.length, 1); // never mutate the persisted artifact
});
test("pending and revision-requested plans stay actionable; a completed round is read-only", () => {
  for (const status of ["pending_confirmation", "revision_requested", "confirmed_in_conversation", "discarded"]) {
    const current = { phase: "plan_review", artifact: plan, workflow: { review_status: status } };
    const done = ["confirmed_in_conversation", "discarded"].includes(status);
    assert.equal(reviewDone(current), done);
    assert.equal(canOperateArtifact(plan, current), !done);
  }
});
test("progress maps stages and failure without invented percentages or content", () => {
  assert.deepEqual(taskSteps(null).map(x => x.status), Array(4).fill("未开始"));
  assert.deepEqual(taskSteps({ phase: "goal_review" }).map(x => x.status), ["已完成", "等待你处理", "未开始", "未开始"]);
  assert.deepEqual(taskSteps({ phase: "research_running" }).map(x => x.status), ["已完成", "已完成", "进行中", "未开始"]);
  assert.equal(taskSteps({ phase: "failed", workflow: { stage: "plan" } })[3].status, "未完成");
  assert.equal(taskSteps({ phase: "failed", workflow: { stage: "research" } })[2].status, "未完成");
  assert.ok(taskSteps({ phase: "plan_review", workflow: { review_status: "confirmed_in_conversation" } }).every(x => x.status === "已完成"));
});

test("weekly progress preserves the failed preparation stage and completed delivery", () => {
  const workflow = { weekly_inputs: {}, weekly_phase: "prepare", stage: "plan" };
  assert.deepEqual(taskSteps({ phase: "failed", workflow }).map(x => x.status),
    ["已完成", "已完成", "未完成", "未开始", "未开始"]);
  assert.ok(taskSteps({ phase: "plan_ready", workflow }).every(x => x.status === "已完成"));
});

// Minimal DOM double: test controller state and side effects without launching a browser or provider.
class Element {
  constructor(tag) { this.tagName = tag; this.children = []; this.dataset = {}; this.listeners = {}; this.attributes = {}; }
  append(...items) {
    for (const item of items) { item.remove(); item.parent = this; this.children.push(item); }
  }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(x => x !== this); this.parent = null; }
  before(item) {
    const parent = this.parent;
    if (!parent) return;
    item.remove(); parent.children.splice(parent.children.indexOf(this), 0, item); item.parent = parent;
  }
  replaceChildren(...items) { for (const item of [...this.children]) item.remove(); this.append(...items); }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(name, handler) { this.listeners[name] = handler; }
  querySelector(selector) { return selector === ".task-progress-body" ? this.children[0] : null; }
  get isConnected() { return !!this.parent; }
}
const descendants = (node) => [node, ...node.children.flatMap(descendants)];
test("goal and research phases never mount result panels, buttons or editors", async (t) => {
  const root = new Element("aside"), messages = new Element("main"); root.append(new Element("div"));
  let current;
  const requests = [];
  t.mock.method(globalThis, "fetch", async (url, options) => {
    requests.push({ url, method: options?.method || "GET" });
    return { ok: true, json: async () => current };
  });
  const previous = globalThis.document;
  globalThis.document = { createElement: tag => new Element(tag), createTextNode: text => Object.assign(new Element("text"), { textContent: text }) };
  t.after(() => { if (previous) globalThis.document = previous; else delete globalThis.document; });
  const controller = mountContentWorkflow(root, { messages, onState() {}, setBusy() {}, reloadConversation() {} });
  for (const phase of ["goal_input", "goal_review", "goal_ready", "research_running", "failed", "interrupted"]) {
    current = { phase, artifact: goal, workflow: { revision: 4, has_save_draft: true },
      research: { research_summary: "研究内容不能注入面板", candidate_topics: [] } };
    controller.update("text-test", false);
    await controller.refresh();
    assert.equal(messages.children.length, 0);
    assert.ok(!descendants(root).some(x => ["button", "input", "textarea", "details"].includes(x.tagName)));
  }
  assert.ok(requests.every(x => x.method === "GET"));
  controller.update(null, false);
  await controller.refresh();
});

test("weekly plan inputs preserve date and personnel without another approval", async (t) => {
  const root = new Element("aside"), messages = new Element("main"); root.append(new Element("div"));
  const current = { phase: "research_ready", workflow: { revision: 2, limitations: ["合成局限"] } };
  t.mock.method(globalThis, "fetch", async () => ({ ok: true, json: async () => current }));
  const previous = globalThis.document;
  globalThis.document = { createElement: tag => new Element(tag), createTextNode: text => Object.assign(new Element("text"), { textContent: text }) };
  t.after(() => { if (previous) globalThis.document = previous; else delete globalThis.document; });
  const controller = mountContentWorkflow(root, { messages, onState() {}, setBusy() {}, reloadConversation() {} });
  controller.update("plan-test", false); await controller.refresh();
  const date = descendants(messages).find(x => x.type === "date");
  const staff = descendants(messages).find(x => x.tagName === "textarea");
  date.value = "2026-09-14"; staff.value = "测试员工甲提交案例照片";
  assert.equal(descendants(messages).some(x => x.type === "checkbox"), false);
  await controller.refresh();
  assert.ok(descendants(messages).includes(date));
  assert.equal(date.value, "2026-09-14"); assert.equal(staff.value, "测试员工甲提交案例照片");
  assert.deepEqual(descendants(messages).filter(x => x.tagName === "button").map(x => x.textContent), ["生成周计划"]);
  controller.update(null, false); await controller.refresh();
});

test("plan revision button sends the exact request through recorded chat", async (t) => {
  const root = new Element("aside"), messages = new Element("main"); root.append(new Element("div"));
  const current = { phase: "plan_ready", workflow: { revision: 3, weekly_saved: { version: 1 } } };
  const sent = [];
  t.mock.method(globalThis, "fetch", async (url, options) => {
    assert.equal(options?.method || "GET", "GET");
    return { ok: true, json: async () => current };
  });
  const previous = globalThis.document;
  globalThis.document = { createElement: tag => new Element(tag) };
  t.after(() => { if (previous) globalThis.document = previous; else delete globalThis.document; });
  const controller = mountContentWorkflow(root, { messages, onState() {}, setBusy() {}, sendChat: text => sent.push(text) });
  controller.update("plan-test", false); await controller.refresh();
  descendants(messages).find(x => x.tagName === "textarea").value = "就用现有照片，别加材料";
  descendants(messages).find(x => x.textContent === "修改周计划").listeners.click();
  assert.deepEqual(sent, ["修改周计划：就用现有照片，别加材料"]);
  controller.update(null, false); await controller.refresh();
});

test("chat submission starts polling before the server enters running state", async (t) => {
  const root = new Element("aside"), messages = new Element("main"); root.append(new Element("div"));
  let current = { phase: "weekly_waiting_research", workflow: { weekly_inputs: {} } };
  const timers = new Map(); let sequence = 0;
  t.mock.method(globalThis, "setTimeout", fn => { const id = ++sequence; timers.set(id, fn); return id; });
  t.mock.method(globalThis, "clearTimeout", id => timers.delete(id));
  t.mock.method(globalThis, "fetch", async () => ({ ok: true, json: async () => current }));
  const previous = globalThis.document;
  globalThis.document = { createElement: tag => new Element(tag), createTextNode: text => Object.assign(new Element("text"), { textContent: text }) };
  t.after(() => { if (previous) globalThis.document = previous; else delete globalThis.document; });
  const controller = mountContentWorkflow(root, { messages, onState() {}, setBusy() {}, reloadConversation() {} });
  controller.update("chat-test", false); await controller.refresh();
  assert.equal(timers.size, 0);
  controller.update("chat-test", true);
  assert.equal(timers.size, 1);
  await [...timers.values()][0]();
  assert.equal(timers.size, 1); // HTTP started, server state has not changed yet.
  current = { ...current, phase: "research_running" };
  await [...timers.values()][0]();
  assert.equal(controller.payload.phase, "research_running");
  current = { ...current, phase: "failed" };
  controller.update("chat-test", false); await controller.refresh();
  assert.equal(timers.size, 0);
  controller.update(null, false); await controller.refresh();
});

test("combined goal fixes the plan form to the confirmed week", async (t) => {
  const root = new Element("aside"), messages = new Element("main"); root.append(new Element("div"));
  const current = { phase: "research_ready", workflow: { revision: 2 }, artifact: {
    ...goal, data: { goal_snapshot: { weekly_goal: { period: { start: "2026-09-21", end: "2026-09-27" } } } }
  } };
  t.mock.method(globalThis, "fetch", async () => ({ ok: true, json: async () => current }));
  const previous = globalThis.document;
  globalThis.document = { createElement: tag => new Element(tag) };
  t.after(() => { if (previous) globalThis.document = previous; else delete globalThis.document; });
  const controller = mountContentWorkflow(root, { messages, onState() {}, setBusy() {}, reloadConversation() {} });
  controller.update("combined-test", false); await controller.refresh();
  const date = descendants(messages).find(x => x.type === "date");
  assert.equal(date.value, "2026-09-21");
  assert.equal(date.readOnly, true);
  controller.update(null, false); await controller.refresh();
});
