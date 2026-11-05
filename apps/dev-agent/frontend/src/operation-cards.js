import { h, reactive } from "vue";

const statusLabels = {
  pending_confirmation: "待确认", confirmed_in_conversation: "已确认",
  revision_requested: "待修改", discarded: "已放弃", approved: "选题已确认",
  partial_success: "部分成功", success: "研究完成", failed: "研究失败",
};
const text = (value) => value == null || value === "" ? "暂无记录" : String(value);
const paragraph = (value) => h("p", { class: "op-copy" }, text(value));
const badge = (value, label) => h("span", {
  class: ["op-badge", ["partial_success", "revision_requested", "failed"].includes(value) ? "op-warning" : ""],
}, label || statusLabels[value] || text(value));
const list = (items) => Array.isArray(items) && items.length
  ? h("ul", { class: "op-list" }, items.map((item) => h("li", text(item)))) : paragraph(items);
const fold = (label, children) => h("details", { class: "op-fold" }, [
  h("summary", label), h("div", { class: "op-fold-body" }, children),
]);
const field = (label, value) => h("div", { class: "op-field" }, [h("h4", label), paragraph(value)]);

function fileInfo(label, path) {
  return fold(label, [paragraph(path), h("button", {
    type: "button", class: "op-button",
    async onClick(event) {
      try {
        await navigator.clipboard.writeText(String(path || ""));
        event.target.textContent = "已复制路径";
      } catch (_) {
        event.target.textContent = "无法复制，请选中上方路径复制";
      }
    },
  }, "复制路径")]);
}

function sources(items) {
  return h("ul", { class: "op-list" }, (items || []).map((item) => {
    // 外部来源仅作链接展示；不执行内容、不允许脚本协议或带密码的 URL。
    let url;
    try {
      url = new URL(item.url);
      if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) url = null;
    } catch (_) { url = null; }
    return h("li", url ? h("a", {
      href: url.href, target: "_blank", rel: "noopener noreferrer", referrerpolicy: "no-referrer",
    }, `${text(item.source_id)} · 查看原始来源`) : `${text(item.source_id)} · 链接不可用`);
  }));
}

function actionEvent(spec, id) {
  const action = spec.actions?.find((item) => item.id === id);
  return action && spec.interaction_mode === "host_action" ? { actionId: id, action } : null;
}

export const OperationCards = {
  props: ["scenario", "spec", "data", "busyAction", "performAction"],
  setup(props) {
    const drafts = reactive({});

    function goalCard() {
      const data = props.data;
      const goal = data.goal_snapshot?.operation_goal || {};
      const pending = data.goal_snapshot?.confirmation?.status === "pending_confirmation";
      const event = actionEvent(props.spec, "confirm_operation_goal");
      return h("section", { class: "operation-card op-goal" }, [
        h("details", { class: "op-goal-content", open: pending }, [
          h("summary", { class: "op-header" }, [
            h("div", [h("h3", "小红书运营目标"), h("p", { class: "op-meta" },
              `${text(data.company_name)} · ${text(data.period)} · ${text(data.version_label)}`)]),
            badge(pending ? "pending_confirmation" : "confirmed_in_conversation"),
          ]),
          h("div", { class: "op-body" }, [
            h("div", { class: "op-field" }, [h("h4", "本期目标（节选）"),
              paragraph(text(data.summary).length > 120 ? `${text(data.summary).slice(0, 120)}…` : data.summary)]),
            h("h4", { class: "op-section-title" }, "本期核心指标"),
            h("div", { class: "op-metrics" }, (data.metrics || []).map((metric) =>
              h("section", { class: "op-metric", key: metric.name }, [
                h("h4", text(metric.name)),
                h("p", { class: "op-target" }, text(metric.target)),
                h("p", { class: "op-meta" }, `历史基线：${text(metric.baseline)}`),
                fold("统计口径与检查方式", [field("统计口径", metric.definition),
                  field("数据来源", metric.data_source), field("检查频率", metric.review_frequency)]),
              ]))),
            fold("完整目标说明", [paragraph(data.summary)]),
            fold("企业背景与目标用户", [field("企业背景", data.company_profile),
              field("业务方向", data.business_direction), field("目标用户", data.target_users)]),
            fold("人员、资源与约束", [list(goal.resources || data.resources),
              field("约束", data.constraints), field("预算情况", data.budget_statement)]),
            fold("目标依据与待验证假设", [h("h4", "目标依据"), list(goal.evidence || data.evidence),
              h("h4", "待验证假设"), list(goal.assumptions || data.assumptions)]),
            h("aside", { class: "op-callout" }, [h("strong", "风险与不确定性"),
              paragraph("目标仍需按统计口径复核，确认目标不代表效果保证或投流获批。"),
              fold("查看完整风险", [list(goal.risks || data.risks)])]),
            fileInfo("目标文件信息", data.goal_ref?.artifact_path),
          ]),
        ]),
        h("footer", { class: "op-footer" }, [paragraph(data.boundary), pending && event
          ? h("button", { type: "button", class: "op-button op-primary", disabled: !!props.busyAction,
            onClick: () => props.performAction(event, {}),
          }, props.busyAction ? "正在确认…" : "确认此版本目标") : null]),
      ]);
    }

    function topicCard(item) {
      const id = item.content_id;
      if (!drafts[id]) drafts[id] = {
        decision: { approved: "确认选题", revision_requested: "要求修改", discarded: "放弃选题" }[item.status] || "确认选题",
        note: item.review_note || "", message: "",
      };
      const draft = drafts[id];
      const event = actionEvent(props.spec, "review_content_topic");
      const missingNote = draft.decision === "要求修改" && !draft.note.trim();
      return h("details", { class: "op-topic", key: id }, [
        h("summary", [h("div", [h("span", { class: "op-meta" }, `${text(item.publish_day)} · ${text(id)}`),
          h("h4", text(item.title))]), badge(item.status, item.status_label)]),
        h("div", { class: "op-topic-body" }, [
          field("目标用户", item.audience), field("预期目标 / 验证假设", item.goal),
          field("企业匹配原因", item.why_fit_company),
          fold("研究依据", [list((item.evidence || []).map((entry) =>
            `${entry.fact}（来源：${(entry.source_refs || []).join("、")}）`))]),
          field("素材需求", (item.required_materials || []).join("；") || item.materials_text),
          h("aside", { class: "op-callout" }, [h("strong", "选题风险"), list(item.risks)]),
          item.review_note ? field("已保存的审核说明", item.review_note) : null,
          event ? h("form", { class: "op-review", onSubmit: async (e) => {
            e.preventDefault();
            if (props.busyAction || missingNote) return;
            draft.message = "";
            const saved = await props.performAction(event, { content_id: id,
              decision: draft.decision, note: draft.note.trim() });
            if (saved) draft.message = "审核已保存";
          } }, [
            h("label", [h("span", "审核决定"), h("select", { "aria-label": "审核决定", value: draft.decision, disabled: !!props.busyAction,
              onChange: (e) => { draft.decision = e.target.value; draft.message = ""; },
            }, ["确认选题", "要求修改", "放弃选题"].map((value) => h("option", { value }, value)))]),
            h("label", [h("span", draft.decision === "要求修改" ? "审核说明（必填）" : "审核说明（选填）"),
              h("textarea", { "aria-label": draft.decision === "要求修改" ? "审核说明（必填）" : "审核说明（选填）",
                value: draft.note, rows: 3, maxlength: 2000, required: draft.decision === "要求修改",
                disabled: !!props.busyAction, placeholder: "需要调整什么？请写下具体要求。",
                onInput: (e) => { draft.note = e.target.value; draft.message = ""; },
              })]),
            h("p", { class: "op-meta", role: "status" }, missingNote ? "要求修改时，请先填写说明。" : draft.message),
            h("button", { class: "op-button op-primary", type: "submit", disabled: !!props.busyAction || missingNote },
              props.busyAction ? "正在保存…" : "保存此选题审核"),
          ]) : null,
        ]),
      ]);
    }

    function planCard() {
      const data = props.data;
      const items = data.contents || [];
      const approved = items.filter((item) => item.status === "approved").length;
      return h("section", { class: "operation-card op-plan" }, [
        h("header", { class: "op-header" }, [
          h("div", [h("h3", "小红书周计划"), h("p", { class: "op-meta" },
            `${text(data.company_name)} · ${text(data.week_start)} 当周 · ${text(data.goal_version_label)}`)]),
          badge(data.approval_status),
        ]),
        h("div", { class: "op-body" }, [
          h("p", { class: "op-progress" }, `${items.length} 个选题 · ${approved} 个已确认 · 展开选题查看详情并审核`),
          h("aside", { class: "op-callout" }, [badge(data.research_execution_status),
            paragraph("请结合研究局限审核选题；确认选题不代表批准制作、发布或投流。"),
            fold("查看研究局限", [list(data.research_limitations || data.limitations_text)])]),
          h("div", { class: "op-topics" }, items.map(topicCard)),
          fold("企业与运营目标", [field("企业背景", data.company_profile), field("运营目标", data.weekly_goal)]),
          fold("原始来源", [sources(data.reference_sources)]),
          fileInfo("研究文件信息", data.research_artifact_path),
        ]),
        h("footer", { class: "op-footer" }, [paragraph(data.rpa_boundary)]),
      ]);
    }
    function draftCard() {
      const data = props.data;
      const id = data.result_ref;
      if (!drafts[id]) drafts[id] = { title: data.title, body: data.body, tags_text: (data.tags || []).join("\n"),
        image_paths_text: (data.image_paths || []).join("\n"), materials_note: "", note: "", message: "", uploading: false };
      const draft = drafts[id];
      const disabled = !!props.busyAction || draft.uploading || data.status === "superseded";
      const executionLocked = ["running", "needs_review", "draft_saved", "manually_confirmed"].includes(data.execution_status);
      const dirty = draft.title !== data.title || draft.body !== data.body || draft.tags_text !== (data.tags || []).join("\n")
        || draft.image_paths_text !== (data.image_paths || []).join("\n") || !!draft.materials_note.trim();
      const action = (label, actionId, values = {}) => {
        const event = actionEvent(props.spec, actionId);
        return event && h("button", { type: "button", class: "op-button",
          disabled: disabled || dirty && ["review_content_draft", "save_content_draft"].includes(actionId)
            || executionLocked && ["edit_content_draft", "review_content_draft", "regenerate_content_draft", "save_content_draft"].includes(actionId),
          onClick: () => props.performAction(event, { result_ref: data.result_ref, version: data.version, ...values }),
        }, label);
      };
      const input = (label, name, rows = 3) => h("label", [h("span", label), h("textarea", {
        value: draft[name], rows, disabled, "aria-label": label,
        onInput: e => { draft[name] = e.target.value; },
      })]);
      return h("section", { class: "operation-card op-draft" }, [
        h("header", { class: "op-header" }, [h("h3", `小红书图文 · 第 ${data.version} 版`), badge(data.status,
          { pending_review: "待审核", approved: "内容已审核", revision_requested: "待修改", superseded: "历史版本" }[data.status])]),
        h("div", { class: "op-body" }, [field("选题", data.topic),
          paragraph("请核对来源是否支持正文事实。修改或补图后先保存新版本，再审核；目标确认与周计划交付不代表批准此正文。"),
          field("执行状态", { not_started: "未执行", running: "执行中", needs_review: "待人工核验",
            draft_saved: "草稿已核验保存", manually_confirmed: "人工确认存在", reviewed_not_saved: "人工确认未保存" }[data.execution_status]
            || data.execution_status), field("图片说明", data.image_notes),
          fold("核对事实与来源", [list((data.claims || []).map(item => `${item.text}（${item.source_refs.join("、")}）`)), sources(data.sources)]),
          fold("研究局限", [list(data.limitations)]),
          data.missing_materials?.length ? field("待补素材", data.missing_materials.join("；")) : null,
          input("标题", "title", 2), input("正文", "body", 8), input("标签（每行一条）", "tags_text"),
          h("label", [h("span", "选择本机图片"), h("input", { type: "file", multiple: true,
            accept: "image/jpeg,image/png,image/webp", disabled,
            async onChange(event) {
              draft.uploading = true; draft.message = "正在保存图片…";
              try {
                const paths = draft.image_paths_text.split("\n").filter(Boolean);
                for (const file of event.target.files) {
                  const response = await fetch(`/api/content-assets/images?filename=${encodeURIComponent(file.name)}`,
                    { method: "POST", headers: { "Content-Type": file.type || "application/octet-stream" }, body: file });
                  const result = await response.json();
                  if (!response.ok) throw new Error(result.detail?.message || "图片上传失败");
                  paths.push(result.path);
                }
                draft.image_paths_text = paths.join("\n"); draft.message = "图片已上传；请保存为新版本后审核。";
              } catch (error) { draft.message = error.message; }
              finally { draft.uploading = false; }
            },
          })]), input("已选图片路径（每行一张）", "image_paths_text"),
          input("素材补齐说明（填写后在新版本记录已补齐）", "materials_note"),
          action("保存修改和图片为新版本", "edit_content_draft", { title: draft.title, body: draft.body,
            tags_text: draft.tags_text, image_paths_text: draft.image_paths_text, materials_note: draft.materials_note }),
          input("审核或重新生成说明", "note"),
          action("批准已保存的此版本", "review_content_draft", { decision: "approve", note: draft.note }),
          action("要求修改", "review_content_draft", { decision: "revise", note: draft.note }),
          action("只重新生成此条", "regenerate_content_draft", { note: draft.note }),
          action("确认启动 RPA 保存草稿", "save_content_draft"),
          action("查询原执行状态（不重复保存）", "reconcile_content_draft"),
          data.execution_status === "needs_review" ? h("div", { class: "op-callout" }, [
            paragraph("请先人工打开草稿箱核对，再填写上方说明并选择结论。确认未保存会建立新待审版本，不会自动再次执行。"),
            action("人工已核对：草稿存在", "resolve_content_draft", { decision: "found", note: draft.note }),
            action("人工已核对：未保存，建立待审新版本", "resolve_content_draft", { decision: "not_saved", note: draft.note }),
          ]) : null,
          h("p", { role: "status" }, draft.message),
          data.execution_result ? field("执行结果", data.execution_result.message) : null,
        ]), h("footer", { class: "op-footer" }, "只保存草稿，不执行最终发布；结果不明时先查询原任务。"),
      ]);
    }
    return () => props.scenario === "operation_goal_confirmation" ? goalCard()
      : props.scenario === "content_draft" ? draftCard() : planCard();
  },
};

export function hasOperationCard(scenario) {
  return ["operation_goal_confirmation", "content_topic_plan", "content_draft"].includes(scenario);
}
