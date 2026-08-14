import { createApp, h, reactive } from "vue";
import PrimeVue from "primevue/config";
import { definePreset } from "@primeuix/themes";
import Aura from "@primeuix/themes/aura";
import "primeicons/primeicons.css";
import { GenerationUiRenderer } from "@lazyai/generation-ui-vue";
import "@lazyai/generation-ui-vue/style.css";

const TestAgentPreset = definePreset(Aura, {
  semantic: {
    primary: {
      50: "#effbf5",
      100: "#d9f4e7",
      200: "#b6e8d2",
      300: "#86d6b4",
      400: "#50ba91",
      500: "#2f9b72",
      600: "#23815b",
      700: "#1f674c",
      800: "#1d523f",
      900: "#194437",
      950: "#0b261e",
    },
  },
});

function clone(value) {
  return value == null ? value : JSON.parse(JSON.stringify(value));
}

function assertArtifact(container, artifact) {
  if (!(container instanceof HTMLElement)) throw new Error("Generation UI 挂载节点无效");
  if (artifact?.type !== "generation_ui") throw new Error("消息不是 Generation UI Artifact");
  if (artifact.spec?.version !== "0.1") throw new Error("仅支持 UISpec 0.1");
  if (artifact.spec?.root?.type !== "Column") throw new Error("UISpec root 必须是 Column");
  if (!["read_only", "host_action"].includes(artifact.spec?.interaction_mode)) {
    throw new Error("当前只允许 read_only 或 host_action Generation UI");
  }
  if (!artifact.data || typeof artifact.data !== "object" || Array.isArray(artifact.data)) {
    throw new Error("Generation UI data 必须是对象");
  }
}

function requiredFields(spec) {
  const required = new Set(
    spec.actions?.flatMap((action) => action.arguments_schema?.required ?? []) ?? [],
  );
  const labels = new Map();
  const stack = [spec.root];
  while (stack.length) {
    const node = stack.pop();
    if (!node || typeof node !== "object") continue;
    const model = node.props?.model;
    if (required.has(model)) {
      labels.set(model, String(node.props.label ?? node.props.text ?? model));
      if (node.props.label && !String(node.props.label).endsWith(" *")) {
        node.props.label = `${node.props.label} *`;
      }
      if (node.props.text && !String(node.props.text).endsWith(" *")) {
        node.props.text = `${node.props.text} *`;
      }
    }
    if (Array.isArray(node.children)) stack.push(...node.children);
  }
  return { required, labels };
}

function missingRequiredFields(requiredState, form) {
  return [...requiredState.required].filter((name) => {
    const value = form[name];
    return value == null
      || (typeof value === "string" && !value.trim())
      || (Array.isArray(value) && value.length === 0);
  });
}

function actionSummary(spec, form) {
  const action = spec.actions?.[0];
  if (action?.id === "transition_owner_briefing_task" && form.action) {
    return `将任务状态更新为：${form.action}`;
  }
  return "";
}

export function mountGenerationUi(container, artifact, options = {}) {
  assertArtifact(container, artifact);
  const spec = clone(artifact.spec);
  const requiredState = requiredFields(spec);
  const state = reactive({
    spec,
    data: clone(artifact.data),
    form: clone(artifact.spec.form_defaults ?? {}),
    busyAction: null,
  });
  const Root = {
    setup() {
      return () => {
        const missing = missingRequiredFields(requiredState, state.form);
        const summary = actionSummary(state.spec, state.form);
        const disabledAction = state.busyAction || (missing.length ? "form-invalid" : null);
        return h("div", { class: "generation-ui-host" }, [
          h(GenerationUiRenderer, {
            spec: state.spec,
            data: state.data,
            form: state.form,
            busyAction: disabledAction,
            showHeader: true,
            "onUpdate:form": (form) => {
              state.form = clone(form);
            },
            onAction: async (event) => {
              if (state.busyAction || missingRequiredFields(requiredState, state.form).length) {
                return;
              }
              if (event.action.confirm && !window.confirm(`确认执行“${event.action.label}”吗？`)) {
                return;
              }
              state.busyAction = event.actionId;
              try {
                const result = await options.onAction?.(clone(event), clone(state.form));
                applyPatch(state.data, result?.data_patch);
                applyPatch(state.form, result?.form_patch);
                applyPatch(state.spec, result?.spec_patch);
              } catch (error) {
                options.onError?.(error);
              } finally {
                state.busyAction = null;
              }
            },
          }),
          state.spec.interaction_mode === "host_action"
            ? h("footer", { class: "generation-ui-host-footer" }, [
              h("button", {
                type: "button",
                class: "generation-ui-cancel-button",
                onClick: () => options.onCancel?.(),
              }, "返回对话"),
              h("div", { class: "generation-ui-action-status", role: "status" }, [
                summary ? h("p", { class: "generation-ui-change-summary" }, summary) : null,
                missing.length
                  ? h("p", { class: "generation-ui-validation-hint" },
                    `请填写必填项：${missing.map((name) => requiredState.labels.get(name) ?? name).join("、")}`)
                  : null,
              ]),
            ])
            : null,
        ]);
      };
    },
  };
  const app = createApp(Root);
  app.use(PrimeVue, {
    theme: { preset: TestAgentPreset, options: { darkModeSelector: false } },
  });
  app.config.errorHandler = (error) => options.onError?.(error);
  app.mount(container);
  container.querySelectorAll(".genui-header p, .ui-form h4").forEach((element) => {
    element.title = element.textContent;
  });

  let destroyed = false;
  return {
    patchForm(patch) {
      if (destroyed) return;
      applyPatch(state.form, patch);
    },
    destroy() {
      if (destroyed) return;
      app.unmount();
      container.replaceChildren();
      destroyed = true;
    },
  };
}

function applyPatch(target, patch) {
  if (!patch || typeof patch !== "object" || Array.isArray(patch)) return;
  Object.entries(patch).forEach(([path, value]) => {
    const parts = path.split(".").filter(Boolean);
    if (!parts.length) return;
    let cursor = target;
    parts.slice(0, -1).forEach((part) => {
      if (!cursor[part] || typeof cursor[part] !== "object" || Array.isArray(cursor[part])) {
        cursor[part] = {};
      }
      cursor = cursor[part];
    });
    cursor[parts.at(-1)] = clone(value);
  });
}
