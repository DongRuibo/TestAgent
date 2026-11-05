import { test } from "node:test";
import assert from "node:assert/strict";
import { createSSRApp, h } from "vue";
import { renderToString } from "@vue/server-renderer";
import { OperationCards, hasOperationCard } from "./operation-cards.js";

const spec = (id, mode = "host_action") => ({ interaction_mode: mode,
  actions: [{ id, label: "测试确认", confirm: true, event_type: "confirm" }] });
function nodes(vnode) {
  if (!vnode || typeof vnode !== "object") return [];
  return [vnode, ...(Array.isArray(vnode.children) ? vnode.children.flatMap(nodes) : [])];
}
function goal(pending = true) {
  return { scenario: "operation_goal_confirmation", spec: spec("confirm_operation_goal", pending ? "host_action" : "read_only"),
    data: { company_name: "测试企业", version_label: "第 1 版", period: "测试周期",
      summary: "较长的完整目标说明。".repeat(40), company_profile: "完整企业背景。".repeat(50),
      boundary: "确认目标不批准投流", metrics: [{ name: "有效线索", target: "24–26", baseline: "20", definition: "去重口径" }],
      goal_snapshot: { confirmation: { status: pending ? "pending_confirmation" : "confirmed_in_conversation" },
        operation_goal: { risks: ["完整风险"], evidence: ["完整依据"], assumptions: ["完整假设"] } },
      goal_ref: { artifact_path: "/test/goal.json" } } };
}
function plan() {
  return { scenario: "content_topic_plan", spec: spec("review_content_topic"), busyAction: null,
    data: { company_name: "测试企业", research_limitations: ["无评论样本"], approval_status: "pending_confirmation",
      research_execution_status: "partial_success", reference_sources: [
        { source_id: "unsafe", url: "javascript:alert(1)" }, { source_id: "safe", url: "https://example.com/post" }],
      contents: ["a", "b"].map((id) => ({ content_id: id, title: `选题 ${id}`, status: "pending_confirmation",
        publish_day: "周一", evidence: [{ fact: "原始证据", source_refs: ["safe"] }], risks: ["素材授权待确认"],
        required_materials: ["真实案例照片"] })) } };
}

test("only the two business scenarios use the new layout", () => {
  assert.equal(hasOperationCard("operation_goal_confirmation"), true);
  assert.equal(hasOperationCard("content_topic_plan"), true);
  assert.equal(hasOperationCard("content_operations"), false);
});
test("old goal data preserves full text, metrics and boundary without a description grid", async () => {
  const props = goal();
  const html = await renderToString(createSSRApp({ render: () => h(OperationCards, props) }));
  for (const value of [props.data.company_profile, props.data.summary, "24–26", "去重口径", "完整风险", "完整假设", "确认目标不批准投流"]) {
    assert.ok(html.includes(value));
  }
  assert.ok(!html.includes("description-list"));
  const confirmed = nodes(OperationCards.setup(goal(false))());
  assert.equal(confirmed.find((n) => n.props?.class === "op-goal-content").props.open, false);
  assert.ok(!confirmed.some((n) => n.children === "确认此版本目标"));
});
test("goal confirmation keeps the existing action and empty arguments", async () => {
  const props = goal();
  let received;
  props.performAction = async (...args) => { received = args; };
  const button = nodes(OperationCards.setup(props)()).find((n) => n.children === "确认此版本目标");
  await button.props.onClick();
  assert.equal(received[0].actionId, "confirm_operation_goal");
  assert.equal(received[0].action.confirm, true);
  assert.deepEqual(received[1], {});
});
test("topic reviews bind the right id and require a nonblank modification note", async () => {
  const props = plan();
  const calls = [];
  props.performAction = async (...args) => { calls.push(args); return true; };
  const render = OperationCards.setup(props);
  let all = nodes(render());
  all.filter((n) => n.type === "select")[1].props.onChange({ target: { value: "要求修改" } });
  all = nodes(render());
  await all.filter((n) => n.type === "form")[1].props.onSubmit({ preventDefault() {} });
  assert.equal(calls.length, 0);
  all.filter((n) => n.type === "textarea")[1].props.onInput({ target: { value: "  补充案例授权  " } });
  await nodes(render()).filter((n) => n.type === "form")[1].props.onSubmit({ preventDefault() {} });
  assert.deepEqual(calls[0][1], { content_id: "b", decision: "要求修改", note: "补充案例授权" });
  assert.equal(nodes(render()).filter((n) => n.type === "textarea")[0].props.value, "");
  props.busyAction = "review_content_topic";
  await nodes(render()).filter((n) => n.type === "form")[0].props.onSubmit({ preventDefault() {} });
  assert.equal(calls.length, 1);
});
test("read-only topics have no review forms; hostile text and URLs cannot execute", async () => {
  const props = plan();
  props.spec.interaction_mode = "read_only";
  props.data.contents[0].title = '<img src=x onerror="alert(1)">';
  const html = await renderToString(createSSRApp({ render: () => h(OperationCards, props) }));
  assert.ok(!html.includes("<form"));
  assert.ok(!html.includes("<img"));
  assert.ok(!html.includes("javascript:"));
  assert.ok(html.includes("&lt;img"));
  assert.ok(html.includes('href="https://example.com/post"'));
  assert.ok(html.includes("无评论样本"));
  assert.ok(html.includes("真实案例照片"));
  assert.ok(!html.includes("<table"));
});

test("an unsuccessful review preserves the draft and does not claim success", async () => {
  const props = plan();
  props.performAction = async () => false;
  const render = OperationCards.setup(props);
  nodes(render()).find((n) => n.type === "textarea").props.onInput({ target: { value: "保留这条说明" } });
  await nodes(render()).find((n) => n.type === "form").props.onSubmit({ preventDefault() {} });
  assert.equal(nodes(render()).find((n) => n.type === "textarea").props.value, "保留这条说明");
  assert.ok(!nodes(render()).some((n) => n.children === "审核已保存"));
});

test("content draft actions bind the saved version and unsaved changes block approval", async () => {
  const calls = [];
  const props = { scenario: "content_draft", busyAction: null, spec: { interaction_mode: "host_action",
    actions: ["edit_content_draft", "review_content_draft", "save_content_draft", "reconcile_content_draft", "regenerate_content_draft"]
      .map(id => ({ id, event_type: "confirm" })) },
    data: { result_ref: "content-draft:synthetic:v2", version: 2, title: "已保存标题", body: "已保存正文", tags: ["收纳"],
      image_paths: ["/synthetic/image.png"], status: "pending_review", execution_status: "not_started", sources: [], claims: [] },
    performAction: async (...args) => { calls.push(args); return true; } };
  assert.equal(hasOperationCard("content_draft"), true);
  const render = OperationCards.setup(props);
  let all = nodes(render());
  await all.find(n => n.children === "批准已保存的此版本").props.onClick();
  assert.deepEqual(calls[0][1], { result_ref: "content-draft:synthetic:v2", version: 2, decision: "approve", note: "" });
  all.find(n => n.type === "textarea" && n.props["aria-label"] === "标题").props.onInput({ target: { value: "尚未保存的标题" } });
  all = nodes(render());
  assert.equal(all.find(n => n.children === "批准已保存的此版本").props.disabled, true);
  assert.equal(all.find(n => n.children === "确认启动 RPA 保存草稿").props.disabled, true);
  assert.equal(all.find(n => n.children === "保存修改和图片为新版本").props.disabled, false);
});

test("uncertain draft offers explicit human resolution while historical versions are disabled", () => {
  const props = { scenario: "content_draft", spec: { interaction_mode: "host_action", actions: [
    { id: "resolve_content_draft" }, { id: "reconcile_content_draft" }] },
    data: { result_ref: "draft:v1", version: 1, title: "标题", body: "正文", tags: [], image_paths: [],
      status: "pending_review", execution_status: "needs_review", sources: [], claims: [] }, performAction() {} };
  let all = nodes(OperationCards.setup(props)());
  assert.ok(all.some(n => n.children === "人工已核对：草稿存在"));
  assert.ok(all.some(n => n.children === "人工已核对：未保存，建立待审新版本"));
  props.data.status = "superseded";
  all = nodes(OperationCards.setup(props)());
  assert.ok(all.filter(n => n.type === "button").every(n => n.props.disabled));
});

test("saved content uses content approval labels and locks actions that cannot be repeated", async () => {
  const props = { scenario: "content_draft", spec: { interaction_mode: "host_action", actions: [
    "edit_content_draft", "review_content_draft", "regenerate_content_draft", "save_content_draft", "reconcile_content_draft",
  ].map(id => ({ id })) }, data: { result_ref: "draft:saved", version: 2, title: "标题", body: "正文", tags: [], image_paths: [],
    status: "approved", execution_status: "draft_saved", sources: [], claims: [] }, performAction() {} };
  const all = nodes(OperationCards.setup(props)());
  for (const label of ["保存修改和图片为新版本", "批准已保存的此版本", "只重新生成此条", "确认启动 RPA 保存草稿"]) {
    assert.equal(all.find(n => n.children === label).props.disabled, true);
  }
  assert.equal(all.find(n => n.children === "查询原执行状态（不重复保存）").props.disabled, false);
  const html = await renderToString(createSSRApp({ render: () => h(OperationCards, props) }));
  assert.ok(html.includes("内容已审核") && html.includes("草稿已核验保存"));
  assert.ok(!html.includes("选题已确认"));
});
