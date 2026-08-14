import { mountGenerationUi } from "./generation-ui/generation-ui.js?v=0.5.2-ux1";
import { isSafeResourceHref, renderMarkdown } from "./markdown.js";

const form = document.querySelector("#chat-form");
const input = document.querySelector("#message-input");
const messages = document.querySelector("#messages");
const sendButton = document.querySelector("#send-button");
const newChatButton = document.querySelector("#new-chat");
const mobileNewChatButton = document.querySelector("#mobile-new-chat");
const deleteChatButton = document.querySelector("#delete-chat");
const resetOwnerDemoButton = document.querySelector("#reset-owner-demo");
const welcomeCard = document.querySelector("#welcome-card");
const attachmentList = document.querySelector("#attachment-list");
const attachmentInput = document.querySelector("#attachment-input");
const attachButton = document.querySelector("#attach-button");
const sidebar = document.querySelector("#sidebar");
const mobileMenuButton = document.querySelector("#mobile-menu-button");
const sidebarCloseButton = document.querySelector("#sidebar-close");
const sidebarBackdrop = document.querySelector("#sidebar-backdrop");
const pageNotice = document.querySelector("#page-notice");
const pageNoticeText = document.querySelector("#page-notice-text");
const retryRestoreButton = document.querySelector("#retry-restore");
const stopButton = document.querySelector("#stop-button");
const sidebarCollapseButton = document.querySelector("#sidebar-collapse");
const chatTitle = document.querySelector("#chat-title");
const dataDisclosureButton = document.querySelector("#data-disclosure-button");
const dataDisclosure = document.querySelector("#data-disclosure");
const attachmentGuidance = document.querySelector("#attachment-guidance");
const conversationList = document.querySelector("#conversation-list");
const historyEmpty = document.querySelector("#history-empty");
const historySearch = document.querySelector("#history-search");
const renameDialog = document.querySelector("#conversation-rename-dialog");
const renameForm = document.querySelector("#conversation-rename-form");
const renameInput = document.querySelector("#conversation-rename-input");
const renameCancelButton = document.querySelector("#conversation-rename-cancel");
const renameBackdrop = document.querySelector("#conversation-rename-backdrop");
const mobileViewport = window.matchMedia("(max-width: 820px)");
const conversationStorageKey = "testagent.conversation_id";
const sidebarCollapsedStorageKey = "testagent.sidebar_collapsed";
const mediaExtensions = new Set([
  "aac", "avi", "flac", "m4a", "mkv", "mov", "mp3", "mp4", "ogg", "wav", "webm",
]);

let conversation = [];
let attachments = [];
let conversationId = null;
let pending = false;
let initializing = true;
let uploading = false;
let attachmentsConfigured = false;
let attachmentPoll = null;
let conversationSummaries = [];
let historyLoadFailed = false;
let historyQuery = "";
let renameConversationId = null;
let activeRequestId = null;
let activeResponseController = null;
let stopRequested = false;
let attachmentRetry = null;
let noticeTimer = null;
const generationUiInstances = new Map();

function showNotice(
  message,
  { retry = false, tone = "warning", autoHideMs = 0 } = {},
) {
  if (noticeTimer) window.clearTimeout(noticeTimer);
  noticeTimer = null;
  pageNoticeText.textContent = message;
  retryRestoreButton.hidden = !retry;
  pageNotice.className = `page-notice ${tone}`;
  pageNotice.hidden = false;
  if (autoHideMs > 0) {
    noticeTimer = window.setTimeout(hideNotice, autoHideMs);
  }
}

function hideNotice() {
  if (noticeTimer) window.clearTimeout(noticeTimer);
  noticeTimer = null;
  pageNotice.hidden = true;
  retryRestoreButton.hidden = true;
}

function removeStoredConversationId() {
  try {
    window.localStorage.removeItem(conversationStorageKey);
  } catch (_) {
    // localStorage 不可用不影响当前页面切换会话。
  }
}

function clearRenderedConversation() {
  generationUiInstances.forEach((instance) => instance.destroy());
  generationUiInstances.clear();
  messages.querySelectorAll(".message-row").forEach((node) => node.remove());
}

function cancelAttachmentPoll() {
  if (attachmentPoll) window.clearTimeout(attachmentPoll);
  attachmentPoll = null;
}

function updateConversationActions() {
  const busy = pending || initializing || uploading;
  newChatButton.disabled = busy;
  mobileNewChatButton.disabled = busy;
  deleteChatButton.disabled = busy || !conversationId;
  resetOwnerDemoButton.disabled = busy;
  conversationList.querySelectorAll(".conversation-item").forEach((button) => {
    button.disabled = busy;
  });
  conversationList.querySelectorAll(".conversation-item-menu-actions button").forEach((button) => {
    button.disabled = busy;
  });
}

function setSidebarOpen(open) {
  document.body.classList.toggle("sidebar-open", open);
  mobileMenuButton.setAttribute("aria-expanded", String(open));
  sidebar.inert = mobileViewport.matches && !open;
  if (open) sidebarCloseButton.focus();
}
const generationUiActionRequests = new Map();

function setSidebarCollapsed(collapsed) {
  document.body.classList.toggle("sidebar-collapsed", collapsed);
  sidebarCollapseButton.setAttribute("aria-expanded", String(!collapsed));
  sidebarCollapseButton.setAttribute(
    "aria-label",
    collapsed ? "展开左侧栏" : "折叠左侧栏",
  );
  sidebarCollapseButton.title = collapsed ? "展开左侧栏" : "折叠左侧栏";
  sidebarCollapseButton.textContent = collapsed ? "›" : "‹";
  try {
    window.localStorage.setItem(sidebarCollapsedStorageKey, String(collapsed));
  } catch (_) {
    // 偏好无法持久化不影响页面使用。
  }
}

function tracePresentation(message) {
  if (message.includes("search_knowledge_base")) return "正在检索知识库";
  if (message.includes("web_search")) return "正在搜索公开网页";
  if (message.includes("browse_public_page") || message.startsWith("Browser:")) {
    return "正在浏览公开网页";
  }
  if (message.includes("analyze_spreadsheet")) return "正在分析表格";
  if (message.includes("generate_business_briefing")) return "正在生成经营简报";
  if (message.includes("present_generation_ui")) return "正在生成界面";
  if (message.includes("run_code")) return "正在运行代码";
  if (message.includes("Memory:")) return "正在处理长期记忆";
  if (message.includes("模型给出最终回答")) return "正在整理回答";
  if (message.includes("调用模型")) return "正在思考";
  return null;
}

function appendProcessStatus(container, message) {
  const label = tracePresentation(message);
  if (!label) return;
  if (container.textContent === label) return;
  container.textContent = label;
}

function renderResources(body, resources) {
  if (!Array.isArray(resources) || !resources.length) return;
  const section = document.createElement("details");
  section.className = "message-resources";
  const heading = document.createElement("summary");
  heading.textContent = `产物与来源 · ${resources.length} 项`;
  const grid = document.createElement("div");
  grid.className = "resource-grid";
  resources.forEach((resource) => {
    const href = resource.href;
    const card = isSafeResourceHref(href)
      ? document.createElement("a")
      : document.createElement("article");
    card.className = `resource-card ${resource.kind}`;
    if (card instanceof HTMLAnchorElement) {
      card.href = href;
      if (resource.kind === "source") {
        card.target = "_blank";
        card.rel = "noopener noreferrer";
      } else {
        card.download = "";
      }
    }
    const type = document.createElement("span");
    type.textContent = resource.kind === "artifact"
      ? "文件"
      : resource.kind === "source" ? "来源" : "附件证据";
    const title = document.createElement("strong");
    title.textContent = resource.title;
    card.append(type, title);
    if (resource.subtitle) {
      const subtitle = document.createElement("small");
      subtitle.textContent = resource.subtitle;
      card.append(subtitle);
    }
    grid.append(card);
  });
  section.append(heading, grid);
  body.append(section);
}

function formatConversationTime(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const elapsed = Math.max(0, Date.now() - date.getTime());
  if (elapsed < 60_000) return "刚刚";
  if (elapsed < 3_600_000) return `${Math.floor(elapsed / 60_000)} 分钟前`;
  if (elapsed < 86_400_000) return `${Math.floor(elapsed / 3_600_000)} 小时前`;
  return new Intl.DateTimeFormat("zh-CN", {
    month: "numeric",
    day: "numeric",
  }).format(date);
}

function conversationTimeGroup(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "更早";
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const yesterday = new Date(today);
  yesterday.setDate(today.getDate() - 1);
  if (date >= today) return "今天";
  if (date >= yesterday) return "昨天";
  return "更早";
}

function updateChatTitle() {
  const summary = conversationSummaries.find(
    (item) => item.conversation_id === conversationId,
  );
  const firstUserMessage = conversation.find((item) => item.role === "user")?.content;
  const fallback = typeof firstUserMessage === "string"
    ? firstUserMessage.replace(/\s+/g, " ").trim()
    : "";
  const title = summary?.title || fallback || "新对话";
  const displayTitle = title.length > 42 ? `${title.slice(0, 42)}…` : title;
  chatTitle.textContent = displayTitle;
  chatTitle.title = title;
}

function renderConversationHistory() {
  conversationList.replaceChildren();
  const filtered = conversationSummaries.filter((item) => (
    !historyQuery || item.title.toLocaleLowerCase().includes(historyQuery)
  ));
  historyEmpty.hidden = filtered.length > 0;
  historyEmpty.textContent = historyLoadFailed
    ? "历史加载失败，请稍后重试"
    : historyQuery ? "没有匹配的历史对话" : "暂无历史对话";
  const busy = pending || initializing || uploading;
  let currentGroup = "";
  filtered.forEach((item) => {
    const group = conversationTimeGroup(item.updated_at);
    if (group !== currentGroup) {
      currentGroup = group;
      const groupLabel = document.createElement("p");
      groupLabel.className = "history-group";
      groupLabel.textContent = group;
      conversationList.append(groupLabel);
    }
    const entry = document.createElement("div");
    entry.className = "conversation-entry";
    entry.setAttribute("role", "listitem");
    const button = document.createElement("button");
    const active = item.conversation_id === conversationId;
    button.type = "button";
    button.className = `conversation-item${active ? " active" : ""}`;
    button.dataset.conversationId = item.conversation_id;
    button.disabled = busy;
    button.title = item.title;
    if (active) button.setAttribute("aria-current", "true");

    const title = document.createElement("strong");
    title.textContent = item.title;
    const meta = document.createElement("small");
    const time = formatConversationTime(item.updated_at);
    meta.textContent = `${item.message_count} 条消息${time ? ` · ${time}` : ""}`;
    button.append(title, meta);

    const menu = document.createElement("details");
    menu.className = "conversation-item-menu";
    const menuToggle = document.createElement("summary");
    menuToggle.setAttribute("aria-label", `管理对话：${item.title}`);
    menuToggle.title = "重命名或删除";
    menuToggle.textContent = "⋯";
    const menuActions = document.createElement("div");
    menuActions.className = "conversation-item-menu-actions";
    const renameButton = document.createElement("button");
    renameButton.type = "button";
    renameButton.dataset.renameConversation = item.conversation_id;
    renameButton.textContent = "重命名";
    renameButton.disabled = busy;
    const removeButton = document.createElement("button");
    removeButton.type = "button";
    removeButton.dataset.deleteConversation = item.conversation_id;
    removeButton.textContent = "删除";
    removeButton.disabled = busy;
    menuActions.append(renameButton, removeButton);
    menu.append(menuToggle, menuActions);
    entry.append(button, menu);
    conversationList.append(entry);
  });
  updateChatTitle();
}

function closeRenameConversation() {
  renameDialog.hidden = true;
  renameConversationId = null;
}

function openRenameConversation(identifier) {
  if (pending || initializing || uploading) return;
  const item = conversationSummaries.find(
    (summary) => summary.conversation_id === identifier,
  );
  if (!item) return;
  renameConversationId = identifier;
  renameInput.value = item.title;
  renameDialog.hidden = false;
  renameInput.focus();
  renameInput.select();
}

async function submitConversationRename(event) {
  event.preventDefault();
  const identifier = renameConversationId;
  const item = conversationSummaries.find(
    (summary) => summary.conversation_id === identifier,
  );
  if (!identifier || !item) return;
  const title = renameInput.value.trim();
  if (!title || title.length > 80) {
    showNotice("对话标题需要包含 1–80 个字符。", { tone: "warning" });
    renameInput.focus();
    return;
  }
  if (title === item.title) {
    closeRenameConversation();
    return;
  }

  initializing = true;
  renameForm.querySelectorAll("button, input").forEach((control) => {
    control.disabled = true;
  });
  updateConversationActions();
  renderConversationHistory();
  try {
    const response = await fetch(`/api/conversations/${identifier}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(extractError(payload, response));
    item.title = payload.title;
    closeRenameConversation();
    renderConversationHistory();
    showNotice("对话已重命名。", { tone: "success", autoHideMs: 2500 });
  } catch (error) {
    showNotice(`重命名失败：${error.message}`, { tone: "error" });
  } finally {
    initializing = false;
    renameForm.querySelectorAll("button, input").forEach((control) => {
      control.disabled = false;
    });
    updateConversationActions();
    renderConversationHistory();
    if (!renameDialog.hidden) renameInput.focus();
  }
}

async function deleteConversationFromHistory(identifier) {
  if (identifier === conversationId) {
    await deleteCurrentConversation();
    return;
  }
  if (pending || initializing || uploading) return;
  const item = conversationSummaries.find(
    (summary) => summary.conversation_id === identifier,
  );
  if (!item || !window.confirm(`确定删除对话“${item.title}”吗？`)) return;

  initializing = true;
  updateConversationActions();
  renderConversationHistory();
  try {
    const response = await fetch(`/api/conversations/${identifier}`, {
      method: "DELETE",
    });
    if (!response.ok && response.status !== 404) {
      const payload = await response.json().catch(() => ({}));
      throw new Error(extractError(payload, response));
    }
    await refreshConversationHistory();
    showNotice("对话已删除。", { tone: "success", autoHideMs: 2500 });
  } catch (error) {
    showNotice(`删除对话失败：${error.message}`, { tone: "error" });
  } finally {
    initializing = false;
    updateConversationActions();
    renderConversationHistory();
  }
}

async function refreshConversationHistory() {
  try {
    const response = await fetch("/api/conversations?limit=50");
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error("conversation history load failed");
    conversationSummaries = Array.isArray(payload.conversations)
      ? payload.conversations
      : [];
    historyLoadFailed = false;
  } catch (_) {
    historyLoadFailed = true;
  }
  renderConversationHistory();
}

try {
  conversationId = window.localStorage.getItem(conversationStorageKey);
} catch (_) {
  conversationId = null;
}

function setTextStatus(elementId, configured) {
  const element = document.querySelector(`#${elementId}`);
  element.textContent = configured ? "已配置" : "未配置";
  element.className = configured ? "ok" : "missing";
}

async function loadHealth() {
  const badge = document.querySelector("#agent-status");
  const mobileBadge = document.querySelector("#mobile-agent-status");
  const webStatus = document.querySelector("#web-status");
  try {
    const response = await fetch("/api/health");
    if (!response.ok) throw new Error("health check failed");
    const health = await response.json();
    webStatus.textContent = "在线";
    webStatus.className = "ok";
    setTextStatus("model-status", health.model_configured);
    setTextStatus("code-status", health.code_sandbox_configured);
    setTextStatus("browser-status", health.browser_sandbox_configured);
    setTextStatus("search-status", health.search_configured);
    setTextStatus("memory-status", health.memory_enabled && health.memory_configured);
    setTextStatus("rag-status", health.rag_enabled && health.rag_configured);
    attachmentsConfigured = Boolean(health.rag_attachments_configured);
    attachButton.title = attachmentsConfigured
      ? "上传当前对话附件"
      : "需要配置 PDF、Office、图片和音视频四个 RAG 附件知识库";
    attachmentGuidance.textContent = attachmentsConfigured
      ? "附件支持 PDF、Office/文本、图片和音视频（≤100 MB）· 仅当前对话"
      : "附件上传暂不可用：需要配置 PDF、Office、图片和音视频四个 RAG 附件知识库。";
    attachmentGuidance.classList.toggle("unavailable", !attachmentsConfigured);
    const configurationComplete = health.status === "ready";
    badge.className = configurationComplete ? "status-pill ready" : "status-pill error";
    badge.innerHTML = `<i></i> ${configurationComplete ? "配置完整" : "配置待补充"}`;
    mobileBadge.className = configurationComplete
      ? "mobile-agent-status ready"
      : "mobile-agent-status error";
    mobileBadge.innerHTML = `<i></i> ${configurationComplete ? "在线" : "待配置"}`;
  } catch (_) {
    webStatus.textContent = "不可用";
    webStatus.className = "missing";
    badge.className = "status-pill error";
    badge.innerHTML = "<i></i> 服务异常";
    mobileBadge.className = "mobile-agent-status error";
    mobileBadge.innerHTML = "<i></i> 异常";
  }
}

async function loadConversation() {
  if (!conversationId) return true;
  try {
    const response = await fetch(`/api/conversations/${conversationId}`);
    if (response.status === 404) {
      conversationId = null;
      conversation = [];
      attachments = [];
      removeStoredConversationId();
      clearRenderedConversation();
      renderAttachments();
      if (welcomeCard) welcomeCard.hidden = false;
      showNotice("原对话已不存在，已切换到一段新对话。", { tone: "warning" });
      renderConversationHistory();
      updateConversationActions();
      return true;
    }
    if (!response.ok) throw new Error("conversation load failed");
    const payload = await response.json();
    conversation = Array.isArray(payload.messages) ? payload.messages : [];
    attachments = Array.isArray(payload.attachments) ? payload.attachments : [];
    clearRenderedConversation();
    conversation.forEach((item) => makeMessage(item.role, item.content, {
      artifacts: item.artifacts,
      resources: item.resources,
      trace: item.trace,
      duration: item.duration_ms,
      context: item.context,
      status: item.status,
      executionDetailsUnavailable: item.role === "assistant"
        && !Array.isArray(item.trace)
        && !Number.isFinite(item.duration_ms)
        && !item.context,
    }));
    renderAttachments();
    scheduleAttachmentPoll();
    if (conversation.length) {
      hideWelcome();
    } else if (welcomeCard) {
      welcomeCard.hidden = false;
    }
    hideNotice();
    renderConversationHistory();
    updateConversationActions();
    return true;
  } catch (_) {
    conversation = [];
    attachments = [];
    renderAttachments();
    showNotice("历史对话暂时无法恢复，会话编号已保留。", {
      retry: true,
      tone: "error",
    });
    updateConversationActions();
    return false;
  }
}

function saveConversationId() {
  try {
    window.localStorage.setItem(conversationStorageKey, conversationId);
  } catch (_) {
    // 服务端仍保存会话；只是本浏览器刷新后无法自动恢复它。
  }
}

async function ensureConversation() {
  if (conversationId) return conversationId;
  const response = await fetch("/api/conversations", { method: "POST" });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(extractError(payload, response));
  conversationId = payload.conversation_id;
  saveConversationId();
  await refreshConversationHistory();
  updateConversationActions();
  return conversationId;
}

function formatBytes(value) {
  if (!Number.isFinite(value)) return "—";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

function attachmentStatus(item) {
  if (item.status === "UPLOADING") return "上传中";
  if (item.status === "PROCESSING") return `解析中 ${Math.round((item.progress || 0) * 100)}%`;
  if (item.status === "READY") return "已可用";
  return item.error_message ? `失败 · ${item.error_message}` : "处理失败";
}

function renderAttachments() {
  attachmentList.replaceChildren();
  attachmentList.hidden = attachments.length === 0;
  attachments.forEach((item) => {
    const card = document.createElement("article");
    card.className = `attachment-card ${item.status.toLowerCase()}`;
    const icon = document.createElement("span");
    icon.className = "attachment-icon";
    icon.textContent = "DOC";
    const body = document.createElement("div");
    body.className = "attachment-body";
    const name = document.createElement("strong");
    name.textContent = item.filename;
    const meta = document.createElement("small");
    meta.textContent = `${formatBytes(item.size_bytes)} · ${attachmentStatus(item)}`;
    body.append(name, meta);
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "attachment-remove";
    remove.setAttribute("aria-label", `移除 ${item.filename}`);
    remove.textContent = "×";
    remove.disabled = uploading || pending;
    if (item.attachment_id) remove.dataset.attachmentId = item.attachment_id;
    if (item.local_id) remove.dataset.localId = item.local_id;
    const actions = document.createElement("div");
    actions.className = "attachment-actions";
    if (item.status === "FAILED") {
      const retry = document.createElement("button");
      retry.type = "button";
      retry.className = "attachment-retry";
      retry.textContent = "重试";
      retry.dataset.retryFilename = item.filename;
      retry.title = "重新选择原文件后重试上传";
      actions.append(retry);
    }
    actions.append(remove);
    card.append(icon, body, actions);
    attachmentList.append(card);
  });
}

async function loadAttachments() {
  if (!conversationId) return;
  const response = await fetch(`/api/conversations/${conversationId}/attachments`);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(extractError(payload, response));
  attachments = Array.isArray(payload.attachments) ? payload.attachments : [];
  renderAttachments();
}

function scheduleAttachmentPoll() {
  if (attachmentPoll) window.clearTimeout(attachmentPoll);
  attachmentPoll = null;
  if (!attachments.some((item) => ["UPLOADING", "PROCESSING"].includes(item.status))) return;
  attachmentPoll = window.setTimeout(async () => {
    try {
      await loadAttachments();
    } catch (_) {
      // 保留现有状态，下一次轮询或刷新时重试。
    }
    scheduleAttachmentPoll();
  }, 3000);
}

function isMediaFile(filename) {
  const extension = filename.toLowerCase().split(".").pop();
  return mediaExtensions.has(extension);
}

async function uploadFile(file) {
  if (isMediaFile(file.name)) {
    const accepted = window.confirm("该音频或视频将由 RAG 解析，并可能把音轨或抽帧发送到硅基流动。是否继续？");
    if (!accepted) return;
  }
  const localId = `local-${Date.now()}-${Math.random()}`;
  attachments.push({
    local_id: localId,
    filename: file.name,
    content_type: file.type || "application/octet-stream",
    size_bytes: file.size,
    status: "UPLOADING",
    progress: 0,
    error_message: null,
  });
  renderAttachments();
  try {
    const identifier = await ensureConversation();
    const headers = { "Content-Type": file.type || "application/octet-stream" };
    if (isMediaFile(file.name)) headers["X-User-Media-Consent"] = "true";
    const response = await fetch(
      `/api/conversations/${identifier}/attachments?filename=${encodeURIComponent(file.name)}`,
      { method: "POST", headers, body: file },
    );
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(extractError(payload, response));
    attachments = attachments.filter((item) => item.local_id !== localId);
    attachments.push(payload);
  } catch (error) {
    attachments = attachments.map((item) => item.local_id === localId
      ? { ...item, status: "FAILED", error_message: error.message }
      : item);
  }
  renderAttachments();
  scheduleAttachmentPoll();
}

async function uploadSelectedFiles(files) {
  if (uploading || !attachmentsConfigured) return;
  uploading = true;
  attachButton.disabled = true;
  updateConversationActions();
  for (const file of files) await uploadFile(file);
  uploading = false;
  attachButton.disabled = !attachmentsConfigured;
  updateConversationActions();
  renderAttachments();
}

async function deleteAttachment(attachmentId) {
  if (!conversationId || pending || uploading) return;
  const response = await fetch(
    `/api/conversations/${conversationId}/attachments/${attachmentId}`,
    { method: "DELETE" },
  );
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(extractError(payload, response));
  }
  attachments = attachments.filter((item) => item.attachment_id !== attachmentId);
  renderAttachments();
}

function hideWelcome() {
  if (welcomeCard) welcomeCard.hidden = true;
}

function contextTrace(context) {
  if (!context) return [];
  const entries = [
    `Context: 历史 ${context.history_message_count} 条，省略 ${context.history_omitted_count} 条`,
    `Context: Memory ${context.memory_policy}，命中 ${context.memory_item_count} 条`,
    `Context: Tool Result ${context.tool_result_count} 条，估算 ${context.estimated_context_chars} 字符`,
  ];
  if (context.context_pack_id) entries.push(`Context Pack: ${context.context_pack_id}`);
  return entries;
}

function makeMessage(role, content, options = {}) {
  const row = document.createElement("article");
  row.className = `message-row ${role}${options.error ? " error" : ""}${options.thinking ? " thinking" : ""}`;
  if (role === "assistant" && !options.error && !options.thinking) {
    row.classList.add("answer");
  }
  if (role === "assistant" && Array.isArray(options.artifacts) && options.artifacts.length) {
    row.classList.add("has-generation-ui");
  }

  const avatar = document.createElement("div");
  avatar.className = "avatar";
  avatar.textContent = role === "user" ? "YOU" : "AI";

  const body = document.createElement("div");
  body.className = "message-content";
  const meta = document.createElement("div");
  meta.className = "message-meta";
  const statusLabel = options.status === "stopped"
    ? " · 已停止"
    : options.status === "failed" ? " · 未完成" : "";
  meta.textContent = `${role === "user" ? "你" : "Mini Agent"}${statusLabel}`;
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
    if (role === "assistant") {
      bubble.classList.add("markdown-body");
      renderMarkdown(bubble, content);
    } else {
      bubble.textContent = content;
    }
  }

  body.append(meta, bubble);
  if (role === "assistant" && options.retryMessage) {
    const retryButton = document.createElement("button");
    retryButton.type = "button";
    retryButton.className = "chat-retry";
    retryButton.textContent = "重新执行";
    retryButton.addEventListener("click", () => {
      row.remove();
      window.setTimeout(() => sendMessage(options.retryMessage), 0);
    });
    body.append(retryButton);
  }
  if (role === "assistant" && Array.isArray(options.artifacts)) {
    options.artifacts.forEach((artifact) => {
      const mountPoint = document.createElement("div");
      mountPoint.className = "generation-ui-mount";
      mountPoint.dataset.artifactId = artifact.artifact_id;
      mountPoint.dataset.scenario = artifact.scenario;
      body.append(mountPoint);
      queueMicrotask(() => {
        try {
          const instance = mountGenerationUi(mountPoint, artifact, {
            async onAction(event, formValues) {
              return submitGenerationUiEvent(artifact, event, formValues);
            },
            onError(error) {
              console.error("Generation UI 渲染失败", error);
              showInlineGenerationUiNotice(
                mountPoint,
                `操作没有完成：${error.message}`,
                "error",
              );
            },
            onCancel() {
              messages.scrollTop = messages.scrollHeight;
              input.focus();
            },
          });
          generationUiInstances.set(artifact.artifact_id, instance);
          attachContentImageUpload(mountPoint, artifact, instance);
          restoreStructuredInputState(mountPoint, artifact);
          window.requestAnimationFrame(() => {
            window.requestAnimationFrame(() => {
              messages.scrollTop = messages.scrollHeight;
            });
          });
        } catch (error) {
          mountPoint.className = "generation-ui-fallback";
          mountPoint.textContent = artifact.text || "界面暂时无法显示，请参考上方文字回答。";
          console.error("Generation UI 挂载失败", error);
        }
      });
    });
  }
  if (role === "assistant") renderResources(body, options.resources);
  const traceEntries = [...(options.trace || []), ...contextTrace(options.context)];
  if (traceEntries.length) {
    const details = document.createElement("details");
    details.className = "trace";
    const summary = document.createElement("summary");
    summary.textContent = `开发调试详情 · ${traceEntries.length} 条记录`;
    if (Number.isFinite(options.duration)) {
      const duration = document.createElement("span");
      duration.className = "duration";
      duration.textContent = `${(options.duration / 1000).toFixed(1)}s`;
      summary.append(duration);
    }
    const list = document.createElement("ol");
    traceEntries.forEach((item) => {
      const entry = document.createElement("li");
      entry.textContent = item;
      list.append(entry);
    });
    details.append(summary, list);
    body.append(details);
  } else if (options.executionDetailsUnavailable) {
    const unavailable = document.createElement("p");
    unavailable.className = "execution-details-unavailable";
    unavailable.textContent = "该回答生成于升级前，未保存执行详情。";
    body.append(unavailable);
  }

  row.append(avatar, body);
  messages.append(row);
  messages.scrollTop = messages.scrollHeight;
  return row;
}

function makeStreamingAnswer(process) {
  process.row.classList.add("streaming");
  process.bubble.hidden = false;
  process.bubble.setAttribute("aria-live", "polite");
  return process;
}

function appendStreamingAnswer(answer, delta) {
  answer.text += delta;
  answer.bubble.textContent = answer.text;
  messages.scrollTop = messages.scrollHeight;
}

function resetStreamingAnswer(answer) {
  answer.text = "";
  answer.bubble.textContent = "";
  answer.bubble.hidden = true;
  answer.row.classList.remove("streaming");
}

function finishStreamingAnswer(answer, content, status = null) {
  answer.text = content;
  answer.status.hidden = true;
  answer.bubble.hidden = false;
  answer.row.classList.remove("streaming", "process-message");
  answer.bubble.classList.add("markdown-body");
  renderMarkdown(answer.bubble, content);
  if (status) {
    answer.row.querySelector(".message-meta").textContent = `Mini Agent · ${status}`;
  }
}

function showInlineGenerationUiNotice(mountPoint, message, tone = "success") {
  let notice = mountPoint.parentElement.querySelector(".generation-ui-notice");
  if (!notice) {
    notice = document.createElement("p");
    notice.className = "generation-ui-notice";
    mountPoint.insertAdjacentElement("afterend", notice);
  }
  notice.className = `generation-ui-notice ${tone}`;
  notice.textContent = message;
}

function disableGenerationUiAction(mountPoint, label) {
  if (!mountPoint || !label) return;
  Array.from(mountPoint.querySelectorAll("button"))
    .filter((button) => button.textContent.includes(label))
    .forEach((button) => { button.disabled = true; });
}

function restoreStructuredInputState(mountPoint, artifact) {
  const submission = artifact.scenario === "structured_input"
    && artifact.data?.status === "已提交"
    && artifact.data?.submission;
  if (!submission || typeof submission !== "object") return;
  const count = Object.keys(submission).length;
  showInlineGenerationUiNotice(mountPoint, `已提交 ${count} 项参数。`);
  const action = (artifact.spec?.actions || [])
    .find((item) => item.id === "submit_structured_input");
  disableGenerationUiAction(mountPoint, action?.label);
}

function appendCompletedAgentResponse(payload) {
  conversationId = payload.conversation_id;
  saveConversationId();
  updateConversationActions();
  conversation.push({
    role: "assistant",
    content: payload.answer,
    artifacts: payload.artifacts || [],
    resources: payload.resources || [],
  });
  makeMessage("assistant", payload.answer, {
    artifacts: payload.artifacts,
    resources: payload.resources,
    trace: payload.trace,
    duration: payload.duration_ms,
    context: payload.context,
  });
  renderConversationHistory();
  void refreshConversationHistory();
}

function makeProcessMessage() {
  const row = document.createElement("article");
  row.className = "message-row assistant process-message";
  const avatar = document.createElement("div");
  avatar.className = "avatar";
  avatar.textContent = "AI";
  const body = document.createElement("div");
  body.className = "message-content";
  const meta = document.createElement("div");
  meta.className = "message-meta";
  meta.textContent = "Mini Agent · 处理中";
  const status = document.createElement("p");
  status.className = "process-status";
  status.textContent = "正在准备任务";
  const bubble = document.createElement("div");
  bubble.className = "bubble process-answer";
  bubble.hidden = true;
  body.append(meta, status, bubble);
  row.append(avatar, body);
  messages.append(row);
  messages.scrollTop = messages.scrollHeight;
  return { row, status, bubble, text: "" };
}

function extractError(payload, response) {
  const detail = payload?.detail;
  if (detail && typeof detail === "object" && detail.message) return detail.message;
  if (typeof detail === "string") return detail;
  return `请求失败（HTTP ${response.status}）`;
}

async function uploadContentImage(file) {
  const response = await fetch(
    `/api/content-assets/images?filename=${encodeURIComponent(file.name)}`,
    {
      method: "POST",
      headers: { "Content-Type": file.type || "application/octet-stream" },
      body: file,
    },
  );
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(extractError(payload, response));
  return payload.path;
}

function attachContentImageUpload(mountPoint, artifact, instance) {
  if (artifact.scenario !== "content_weekly_plan") return;
  const field = mountPoint.querySelector('[data-node-id="review-image-paths"]');
  const textarea = field?.querySelector("textarea");
  if (!field || !textarea || typeof instance.patchForm !== "function") return;

  const control = document.createElement("div");
  control.className = "content-image-upload";
  const picker = document.createElement("input");
  picker.type = "file";
  picker.accept = ".jpg,.jpeg,.png,.webp,image/jpeg,image/png,image/webp";
  picker.multiple = true;
  picker.hidden = true;
  const button = document.createElement("button");
  button.type = "button";
  button.className = "content-image-upload-button";
  button.textContent = "选择/上传图片";
  const status = document.createElement("small");
  status.className = "content-image-upload-status";
  status.setAttribute("role", "status");
  status.textContent = "支持一次选择 1～9 张 JPEG、PNG 或 WebP";
  control.append(picker, button, status);
  field.insertBefore(control, textarea);

  button.addEventListener("click", (event) => {
    event.preventDefault();
    event.stopPropagation();
    picker.click();
  });
  picker.addEventListener("change", async (event) => {
    event.stopPropagation();
    const files = Array.from(picker.files || []);
    if (!files.length) return;
    const paths = textarea.value.split("\n").map((item) => item.trim()).filter(Boolean);
    if (paths.length + files.length > 9) {
      status.className = "content-image-upload-status error";
      status.textContent = `最多 9 张；当前已有 ${paths.length} 张，本次选择了 ${files.length} 张。`;
      picker.value = "";
      return;
    }

    button.disabled = true;
    status.className = "content-image-upload-status";
    try {
      for (const file of files) {
        status.textContent = `正在上传 ${file.name}…`;
        const path = await uploadContentImage(file);
        paths.push(path);
        instance.patchForm({ image_paths_text: paths.join("\n") });
      }
      status.className = "content-image-upload-status success";
      status.textContent = `已上传并自动填写 ${paths.length} 张图片。`;
    } catch (error) {
      status.className = "content-image-upload-status error";
      status.textContent = `图片上传失败：${error.message}`;
    } finally {
      button.disabled = false;
      picker.value = "";
    }
  });
}

async function submitGenerationUiEvent(artifact, event, formValues) {
  const requestKey = `${artifact.artifact_id}:${event.actionId}:${JSON.stringify(formValues)}`;
  const existing = generationUiActionRequests.get(requestKey);
  if (existing) return existing;
  const host = document.querySelector(
    `.generation-ui-mount[data-artifact-id="${artifact.artifact_id}"]`,
  );
  if (
    host
    && artifact.scenario === "structured_input"
    && event.actionId === "submit_structured_input"
  ) {
    showInlineGenerationUiNotice(host, "参数已提交，Agent 正在计算…");
  }
  const request = (async () => {
    const response = await fetch("/api/generation/event", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        conversation_id: artifact.conversation_id,
        message_id: Number(artifact.message_id),
        artifact_id: artifact.artifact_id,
        scenario: artifact.scenario,
        event_type: event.action.event_type
          || (event.action.confirm ? "confirm" : "action"),
        action_id: event.actionId,
        arguments: formValues,
        idempotency_key: crypto.randomUUID(),
      }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(extractError(payload, response));
    return payload;
  })();
  generationUiActionRequests.set(requestKey, request);
  try {
    const payload = await request;
    if (host) showInlineGenerationUiNotice(host, payload.message, "success");
    if (payload.continuation) {
      disableGenerationUiAction(host, event.action?.label);
      appendCompletedAgentResponse(payload.continuation);
    }
    return payload;
  } catch (error) {
    generationUiActionRequests.delete(requestKey);
    if (host) showInlineGenerationUiNotice(host, `处理失败：${error.message}`, "error");
    throw error;
  }
}

async function readNdjson(response, onEvent) {
  if (!response.body) throw new Error("浏览器不支持流式读取回答。");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
    const lines = buffer.split("\n");
    buffer = lines.pop() || "";
    for (const line of lines) {
      if (line.trim()) onEvent(JSON.parse(line));
    }
    if (done) break;
  }
  if (buffer.trim()) onEvent(JSON.parse(buffer));
}

async function stopCurrentRequest() {
  if (!activeRequestId) return;
  stopButton.disabled = true;
  try {
    const response = await fetch(`/api/chat/stream/${activeRequestId}`, {
      method: "DELETE",
    });
    if (!response.ok && response.status !== 404) {
      const payload = await response.json().catch(() => ({}));
      throw new Error(extractError(payload, response));
    }
    stopRequested = true;
    stopButton.querySelector("span").textContent = "正在停止";
    showNotice("正在停止生成；已发送的问题和已生成的内容会保留。", {
      tone: "warning",
    });
  } catch (error) {
    stopButton.disabled = false;
    showNotice(`停止失败：${error.message}`, { tone: "error" });
  }
}

async function sendMessage(text) {
  const message = text.trim();
  if (pending || initializing || !message) return;
  hideNotice();
  if (attachments.some((item) => ["UPLOADING", "PROCESSING"].includes(item.status))) {
    makeMessage("assistant", "附件仍在解析，请等待状态变为“已可用”后再提问。", { error: true });
    return;
  }
  pending = true;
  input.value = "";
  resizeInput();
  hideWelcome();
  conversation.push({ role: "user", content: message });
  updateChatTitle();
  makeMessage("user", message);
  const process = makeProcessMessage();
  let streamedAnswer = null;
  activeRequestId = crypto.randomUUID();
  activeResponseController = new AbortController();
  sendButton.disabled = true;
  sendButton.hidden = true;
  stopButton.hidden = false;
  stopButton.disabled = false;
  input.disabled = true;
  attachButton.disabled = true;
  updateConversationActions();
  renderAttachments();

  try {
    let payload = null;
    let streamError = null;
    let cancelled = false;
    const response = await fetch("/api/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        request_id: activeRequestId,
        message,
        conversation_id: conversationId,
      }),
      signal: activeResponseController.signal,
    });
    if (!response.ok) {
      const errorPayload = await response.json().catch(() => ({}));
      throw new Error(extractError(errorPayload, response));
    }
    await readNdjson(response, (event) => {
      if (event.type === "status") appendProcessStatus(process.status, event.message);
      if (event.type === "accepted" && event.conversation_id) {
        conversationId = event.conversation_id;
        saveConversationId();
        updateConversationActions();
        void refreshConversationHistory();
      }
      if (event.type === "answer_delta") {
        if (!streamedAnswer) streamedAnswer = makeStreamingAnswer(process);
        appendStreamingAnswer(streamedAnswer, event.delta || "");
      }
      if (event.type === "answer_reset" && streamedAnswer) {
        resetStreamingAnswer(streamedAnswer);
        streamedAnswer = null;
      }
      if (event.type === "result") payload = event.data;
      if (event.type === "cancelled") {
        cancelled = true;
        if (event.conversation_id) {
          conversationId = event.conversation_id;
          saveConversationId();
        }
      }
      if (event.type === "error") {
        streamError = event.detail || { message: "请求失败" };
      }
    });
    if (cancelled) {
      if (streamedAnswer) {
        finishStreamingAnswer(streamedAnswer, streamedAnswer.text, "已停止");
      } else {
        process.row.remove();
      }
      await loadConversation();
      await refreshConversationHistory();
      showNotice("已停止生成；问题和已生成的内容已保留。", {
        tone: "warning",
        autoHideMs: 4000,
      });
      return;
    }
    if (streamError) {
      process.row.remove();
      const error = new Error(
        streamError.message || streamError.error_type || "请求失败",
      );
      error.retryable = streamError.retryable === true;
      throw error;
    }
    if (!payload) throw new Error("Agent 没有返回完整结果。");
    process.row.remove();
    appendCompletedAgentResponse(payload);
  } catch (error) {
    if (stopRequested || error.name === "AbortError") {
      if (streamedAnswer) {
        finishStreamingAnswer(streamedAnswer, streamedAnswer.text, "已停止");
      } else {
        process.row.remove();
      }
      if (conversationId) await loadConversation();
      await refreshConversationHistory();
      showNotice("已停止生成；问题和已生成的内容已保留。", {
        tone: "warning",
        autoHideMs: 4000,
      });
      return;
    }
    process.row.remove();
    if (conversationId) await loadConversation();
    makeMessage("assistant", `这次任务没有完成：${error.message}`, {
      error: true,
      retryMessage: error.retryable === true ? message : null,
    });
  } finally {
    activeRequestId = null;
    activeResponseController = null;
    stopRequested = false;
    pending = false;
    sendButton.disabled = false;
    sendButton.hidden = false;
    stopButton.hidden = true;
    stopButton.disabled = false;
    stopButton.querySelector("span").textContent = "停止生成";
    input.disabled = false;
    attachButton.disabled = !attachmentsConfigured;
    updateConversationActions();
    renderAttachments();
    input.focus();
  }
}

function resizeInput() {
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 150)}px`;
}

function resetConversationView() {
  conversationId = null;
  conversation = [];
  attachments = [];
  cancelAttachmentPoll();
  removeStoredConversationId();
  clearRenderedConversation();
  renderAttachments();
  if (welcomeCard) welcomeCard.hidden = false;
  renderConversationHistory();
  updateConversationActions();
}

async function switchConversation(identifier) {
  if (
    pending
    || initializing
    || uploading
    || !identifier
    || identifier === conversationId
  ) return;

  initializing = true;
  conversationId = identifier;
  conversation = [];
  attachments = [];
  cancelAttachmentPoll();
  saveConversationId();
  clearRenderedConversation();
  renderAttachments();
  hideNotice();
  renderConversationHistory();
  input.disabled = true;
  sendButton.disabled = true;
  attachButton.disabled = true;
  updateConversationActions();

  const restored = await loadConversation();
  await refreshConversationHistory();
  initializing = false;
  input.disabled = false;
  sendButton.disabled = false;
  attachButton.disabled = !attachmentsConfigured;
  updateConversationActions();
  if (restored && mobileViewport.matches) setSidebarOpen(false);
  input.focus();
}

function startNewConversation() {
  if (pending || initializing || uploading) return;
  const preservedPreviousConversation = Boolean(conversationId);
  resetConversationView();
  setSidebarOpen(false);
  showNotice(
    preservedPreviousConversation
      ? "已开始新对话；上一段对话仍保存在本机服务中。"
      : "已准备一段新对话。",
    { tone: "success", autoHideMs: 3000 },
  );
  input.focus();
}

async function deleteCurrentConversation() {
  if (pending || initializing || uploading || !conversationId) return;
  const confirmed = window.confirm(
    "确定删除当前对话吗？消息和当前对话附件会被删除，长期记忆不会随之删除。",
  );
  if (!confirmed) return;

  pending = true;
  sendButton.disabled = true;
  input.disabled = true;
  attachButton.disabled = true;
  updateConversationActions();
  const identifier = conversationId;
  try {
    const response = await fetch(`/api/conversations/${identifier}`, { method: "DELETE" });
    if (!response.ok && response.status !== 404) {
      const payload = await response.json().catch(() => ({}));
      throw new Error(extractError(payload, response));
    }
    resetConversationView();
    await refreshConversationHistory();
    setSidebarOpen(false);
    showNotice("当前对话及其附件已删除，长期记忆未删除。", { tone: "success" });
  } catch (error) {
    showNotice(`删除当前对话失败：${error.message}`, { tone: "error" });
  } finally {
    pending = false;
    sendButton.disabled = false;
    input.disabled = false;
    attachButton.disabled = !attachmentsConfigured;
    updateConversationActions();
    input.focus();
  }
}

async function resetOwnerBriefingDemo() {
  if (pending || initializing || uploading) return;
  if (!window.confirm(
    "确定重置老板早报演示吗？这会删除全部老板早报演示对话、任务、提醒和操作审计。",
  )) return;
  if (!window.confirm(
    "最后确认：该操作不可在页面撤销，但不会影响其他类型的对话。",
  )) return;

  pending = true;
  input.disabled = true;
  sendButton.disabled = true;
  attachButton.disabled = true;
  updateConversationActions();
  try {
    const response = await fetch("/api/owner/demo-reset", { method: "POST" });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(extractError(payload, response));
    resetConversationView();
    await refreshConversationHistory();
    setSidebarOpen(false);
    showNotice(
      `老板早报演示已重置：删除 ${payload.deleted_conversation_count || 0} 个对话、${payload.deleted_task_count || 0} 个任务。`,
      { tone: "success", autoHideMs: 5000 },
    );
  } catch (error) {
    showNotice(`重置老板早报演示失败：${error.message}`, { tone: "error" });
  } finally {
    pending = false;
    input.disabled = false;
    sendButton.disabled = false;
    attachButton.disabled = !attachmentsConfigured;
    updateConversationActions();
    input.focus();
  }
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  sendMessage(input.value);
});

input.addEventListener("keydown", (event) => {
  if (event.isComposing || event.keyCode === 229) return;
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    form.requestSubmit();
  }
});
input.addEventListener("input", resizeInput);

document.querySelectorAll(".prompt-chip").forEach((button) => {
  button.addEventListener("click", () => sendMessage(button.dataset.prompt || ""));
});

attachButton.addEventListener("click", () => {
  if (!pending && !initializing && !uploading && attachmentsConfigured) attachmentInput.click();
});

attachmentInput.addEventListener("change", () => {
  const files = Array.from(attachmentInput.files || []);
  attachmentInput.value = "";
  if (attachmentRetry) {
    const retry = attachmentRetry;
    attachmentRetry = null;
    const file = files[0];
    if (!file) return;
    if (file.name !== retry.filename) {
      showNotice(`请选择原文件“${retry.filename}”后重试。`, { tone: "warning" });
      return;
    }
    const prepareRetry = async () => {
      if (retry.attachmentId) await deleteAttachment(retry.attachmentId);
      if (retry.localId) {
        attachments = attachments.filter((item) => item.local_id !== retry.localId);
      }
      await uploadSelectedFiles([file]);
    };
    void prepareRetry().catch((error) => {
      showNotice(`附件重试失败：${error.message}`, { tone: "error" });
    });
    return;
  }
  void uploadSelectedFiles(files);
});

attachmentList.addEventListener("click", async (event) => {
  const retryButton = event.target.closest("button[data-retry-filename]");
  if (retryButton) {
    const card = retryButton.closest(".attachment-card");
    attachmentRetry = {
      filename: retryButton.dataset.retryFilename,
      attachmentId: card.querySelector("button[data-attachment-id]")?.dataset.attachmentId,
      localId: card.querySelector("button[data-local-id]")?.dataset.localId,
    };
    attachmentInput.click();
    return;
  }
  const button = event.target.closest("button[data-attachment-id]");
  const localButton = event.target.closest("button[data-local-id]");
  if (!button && !localButton) return;
  if (localButton) {
    attachments = attachments.filter((item) => item.local_id !== localButton.dataset.localId);
    renderAttachments();
    return;
  }
  button.disabled = true;
  try {
    await deleteAttachment(button.dataset.attachmentId);
  } catch (error) {
    makeMessage("assistant", `移除附件失败：${error.message}`, { error: true });
    button.disabled = false;
  }
});

newChatButton.addEventListener("click", startNewConversation);
mobileNewChatButton.addEventListener("click", startNewConversation);
deleteChatButton.addEventListener("click", deleteCurrentConversation);
resetOwnerDemoButton.addEventListener("click", resetOwnerBriefingDemo);
stopButton.addEventListener("click", stopCurrentRequest);
sidebarCollapseButton.addEventListener("click", () => {
  setSidebarCollapsed(!document.body.classList.contains("sidebar-collapsed"));
});
dataDisclosureButton.addEventListener("click", () => {
  const open = dataDisclosure.hidden;
  dataDisclosure.hidden = !open;
  dataDisclosureButton.setAttribute("aria-expanded", String(open));
});

conversationList.addEventListener("click", (event) => {
  const renameButton = event.target.closest("button[data-rename-conversation]");
  if (renameButton) {
    openRenameConversation(renameButton.dataset.renameConversation);
    return;
  }
  const deleteButton = event.target.closest("button[data-delete-conversation]");
  if (deleteButton) {
    void deleteConversationFromHistory(deleteButton.dataset.deleteConversation);
    return;
  }
  const button = event.target.closest("button[data-conversation-id]");
  if (button) void switchConversation(button.dataset.conversationId);
});
historySearch.addEventListener("input", () => {
  historyQuery = historySearch.value.trim().toLocaleLowerCase();
  renderConversationHistory();
});
renameForm.addEventListener("submit", submitConversationRename);
renameCancelButton.addEventListener("click", closeRenameConversation);
renameBackdrop.addEventListener("click", closeRenameConversation);

mobileMenuButton.addEventListener("click", () => setSidebarOpen(true));
sidebarCloseButton.addEventListener("click", () => setSidebarOpen(false));
sidebarBackdrop.addEventListener("click", () => setSidebarOpen(false));
mobileViewport.addEventListener("change", () => {
  if (!mobileViewport.matches) document.body.classList.remove("sidebar-open");
  sidebar.inert = mobileViewport.matches
    && !document.body.classList.contains("sidebar-open");
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !renameDialog.hidden) {
    closeRenameConversation();
    return;
  }
  if (event.key === "Escape" && document.body.classList.contains("sidebar-open")) {
    setSidebarOpen(false);
    mobileMenuButton.focus();
  }
});

retryRestoreButton.addEventListener("click", async () => {
  retryRestoreButton.disabled = true;
  await loadConversation();
  retryRestoreButton.disabled = false;
});

async function initialize() {
  let sidebarCollapsed = false;
  try {
    sidebarCollapsed = window.localStorage.getItem(sidebarCollapsedStorageKey) === "true";
  } catch (_) {
    sidebarCollapsed = false;
  }
  setSidebarCollapsed(sidebarCollapsed);
  setSidebarOpen(false);
  input.disabled = true;
  sendButton.disabled = true;
  attachButton.disabled = true;
  await Promise.all([loadHealth(), loadConversation(), refreshConversationHistory()]);
  initializing = false;
  input.disabled = false;
  sendButton.disabled = false;
  attachButton.disabled = !attachmentsConfigured;
  updateConversationActions();
  input.focus();
}

initialize();

window.addEventListener("beforeunload", () => {
  activeResponseController?.abort();
});
