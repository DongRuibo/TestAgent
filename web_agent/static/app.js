const form = document.querySelector("#chat-form");
const input = document.querySelector("#message-input");
const messages = document.querySelector("#messages");
const sendButton = document.querySelector("#send-button");
const clearButton = document.querySelector("#clear-chat");
const welcomeCard = document.querySelector("#welcome-card");

let conversation = [];
let pending = false;

function setTextStatus(elementId, configured) {
  const element = document.querySelector(`#${elementId}`);
  element.textContent = configured ? "已配置" : "未配置";
  element.className = configured ? "ok" : "missing";
}

async function loadHealth() {
  const badge = document.querySelector("#agent-status");
  try {
    const response = await fetch("/api/health");
    if (!response.ok) throw new Error("health check failed");
    const health = await response.json();
    setTextStatus("model-status", health.model_configured);
    setTextStatus("code-status", health.code_sandbox_configured);
    setTextStatus("browser-status", health.browser_sandbox_configured);
    const ready = health.status === "ready";
    badge.className = ready ? "status-pill ready" : "status-pill error";
    badge.innerHTML = `<i></i> ${ready ? "Agent 就绪" : "需要配置"}`;
  } catch (_) {
    badge.className = "status-pill error";
    badge.innerHTML = "<i></i> 服务异常";
  }
}

function hideWelcome() {
  if (welcomeCard) welcomeCard.hidden = true;
}

function makeMessage(role, content, options = {}) {
  const row = document.createElement("article");
  row.className = `message-row ${role}${options.error ? " error" : ""}${options.thinking ? " thinking" : ""}`;

  const avatar = document.createElement("div");
  avatar.className = "avatar";
  avatar.textContent = role === "user" ? "YOU" : "AI";

  const body = document.createElement("div");
  body.className = "message-content";
  const meta = document.createElement("div");
  meta.className = "message-meta";
  meta.textContent = role === "user" ? "你" : "Mini Agent";
  const bubble = document.createElement("div");
  bubble.className = "bubble";

  if (options.thinking) {
    bubble.setAttribute("aria-label", "Agent 正在思考");
    for (let index = 0; index < 3; index += 1) {
      const dot = document.createElement("span");
      dot.className = "thinking-dot";
      bubble.append(dot);
    }
  } else {
    bubble.textContent = content;
  }

  body.append(meta, bubble);
  if (options.trace?.length) {
    const details = document.createElement("details");
    details.className = "trace";
    const summary = document.createElement("summary");
    summary.textContent = `查看执行轨迹 · ${options.trace.length} 条记录`;
    if (Number.isFinite(options.duration)) {
      const duration = document.createElement("span");
      duration.className = "duration";
      duration.textContent = `${(options.duration / 1000).toFixed(1)}s`;
      summary.append(duration);
    }
    const list = document.createElement("ol");
    options.trace.forEach((item) => {
      const entry = document.createElement("li");
      entry.textContent = item;
      list.append(entry);
    });
    details.append(summary, list);
    body.append(details);
  }

  row.append(avatar, body);
  messages.append(row);
  messages.scrollTop = messages.scrollHeight;
  return row;
}

function extractError(payload, response) {
  const detail = payload?.detail;
  if (detail && typeof detail === "object" && detail.message) return detail.message;
  if (typeof detail === "string") return detail;
  return `请求失败（HTTP ${response.status}）`;
}

async function sendMessage(text) {
  const message = text.trim();
  if (pending || !message) return;
  pending = true;
  input.value = "";
  resizeInput();
  hideWelcome();
  const priorHistory = conversation.slice(-20);
  conversation.push({ role: "user", content: message });
  makeMessage("user", message);
  const thinking = makeMessage("assistant", "", { thinking: true });
  sendButton.disabled = true;
  input.disabled = true;

  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message, history: priorHistory }),
    });
    const payload = await response.json().catch(() => ({}));
    thinking.remove();
    if (!response.ok) throw new Error(extractError(payload, response));
    conversation.push({ role: "assistant", content: payload.answer });
    makeMessage("assistant", payload.answer, {
      trace: payload.trace,
      duration: payload.duration_ms,
    });
  } catch (error) {
    thinking.remove();
    conversation.pop();
    makeMessage("assistant", `这次任务没有完成：${error.message}`, { error: true });
  } finally {
    pending = false;
    sendButton.disabled = false;
    input.disabled = false;
    input.focus();
  }
}

function resizeInput() {
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 150)}px`;
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  sendMessage(input.value);
});

input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    form.requestSubmit();
  }
});
input.addEventListener("input", resizeInput);

document.querySelectorAll(".prompt-chip").forEach((button) => {
  button.addEventListener("click", () => sendMessage(button.dataset.prompt || ""));
});

clearButton.addEventListener("click", () => {
  conversation = [];
  messages.querySelectorAll(".message-row").forEach((node) => node.remove());
  if (welcomeCard) welcomeCard.hidden = false;
  input.focus();
});

loadHealth();
input.focus();
