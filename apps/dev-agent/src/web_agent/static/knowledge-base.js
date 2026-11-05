const summaryTitle = document.querySelector("#summary-title");
const summaryDescription = document.querySelector("#summary-description");
const summaryStatus = document.querySelector("#summary-status");
const summaryCount = document.querySelector("#summary-count");
const summaryProfile = document.querySelector("#summary-profile");
const uploadForm = document.querySelector("#upload-form");
const uploadButton = document.querySelector("#upload-button");
const fileInput = document.querySelector("#file-input");
const fileLabel = document.querySelector("#file-label");
const mediaConsent = document.querySelector("#media-consent");
const mediaConsentInput = document.querySelector("#media-consent-input");
const documentList = document.querySelector("#document-list");
const emptyState = document.querySelector("#empty-state");
const refreshButton = document.querySelector("#refresh-button");
const notice = document.querySelector("#notice");
const pagination = document.querySelector("#pagination");
const previousPage = document.querySelector("#previous-page");
const nextPage = document.querySelector("#next-page");
const pageLabel = document.querySelector("#page-label");

const mediaExtensions = new Set([
  "aac", "avi", "flac", "m4a", "mkv", "mov", "mp3", "mp4", "ogg", "wav", "webm",
]);
const activeStatuses = new Set([
  "RECEIVED", "UPLOADING", "UPLOADED", "QUEUED", "PARSING", "INDEXING", "RETRYING", "DELETING",
]);
const statusLabels = {
  RECEIVED: "已接收", UPLOADING: "上传中", UPLOADED: "已上传", QUEUED: "排队中",
  PARSING: "解析中", INDEXING: "建立索引", READY: "已可用", FAILED: "失败",
  RETRYING: "重试中", DELETING: "删除中", DELETE_FAILED: "删除失败", DELETED: "已删除",
};

let currentPage = 1;
let pageSize = 50;
let pollTimer = null;
let busy = false;

function showNotice(message, tone = "success") {
  notice.textContent = message;
  notice.className = `notice${tone === "error" ? " error" : ""}`;
  notice.hidden = false;
}

async function requestJson(url, options = {}) {
  const response = await fetch(url, options);
  let payload = {};
  try { payload = await response.json(); } catch (_) { /* 使用通用错误。 */ }
  if (!response.ok) {
    const detail = payload.detail || {};
    throw new Error(detail.message || detail.error_type || `请求失败（HTTP ${response.status}）`);
  }
  return payload;
}

function formatBytes(value) {
  if (!Number.isFinite(value)) return "—";
  if (value < 1024) return `${value} B`;
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 ** 2).toFixed(1)} MB`;
}

function formatTime(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString("zh-CN", { hour12: false });
}

function statusClass(status) {
  if (status === "READY") return "ready";
  if (status === "FAILED" || status === "DELETE_FAILED") return "failed";
  return "processing";
}

function renderDocuments(documents, meta) {
  documentList.replaceChildren();
  emptyState.hidden = documents.length !== 0;
  let hasActiveDocument = false;
  documents.forEach((item) => {
    hasActiveDocument ||= activeStatuses.has(item.status);
    const row = document.createElement("article");
    row.className = "document-row";

    const main = document.createElement("div");
    main.className = "document-main";
    const name = document.createElement("strong");
    name.textContent = item.filename;
    name.title = item.filename;
    const metaText = document.createElement("small");
    metaText.textContent = `${formatBytes(item.size_bytes)} · 更新于 ${formatTime(item.updated_at)}`;
    main.append(name, metaText);

    const middle = document.createElement("div");
    const status = document.createElement("div");
    status.className = `document-status ${statusClass(item.status)}`;
    const dot = document.createElement("span");
    dot.className = "status-dot";
    const label = document.createElement("span");
    label.textContent = statusLabels[item.status] || item.status;
    status.append(dot, label);
    const stats = document.createElement("div");
    stats.className = "document-stats";
    stats.textContent = item.status === "FAILED" && item.error_code
      ? `错误：${item.error_code}`
      : `${item.chunk_count ?? 0} Chunks · ${item.token_count ?? 0} Tokens`;
    const progress = document.createElement("div");
    progress.className = "progress-track";
    const progressBar = document.createElement("span");
    progressBar.style.width = `${Math.max(0, Math.min(1, item.progress ?? 0)) * 100}%`;
    progress.append(progressBar);
    middle.append(status, stats, progress);

    const actions = document.createElement("div");
    actions.className = "document-actions";
    if (item.status === "FAILED") {
      const retry = document.createElement("button");
      retry.type = "button";
      retry.className = "retry";
      retry.dataset.action = "retry";
      retry.dataset.documentId = item.document_id;
      retry.textContent = "重试";
      actions.append(retry);
    }
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "delete";
    remove.dataset.action = "delete";
    remove.dataset.documentId = item.document_id;
    remove.dataset.filename = item.filename;
    remove.textContent = item.status === "DELETING" ? "删除中" : "删除";
    remove.disabled = item.status === "DELETING";
    actions.append(remove);
    row.append(main, middle, actions);
    documentList.append(row);
  });

  const total = Number(meta.total ?? documents.length);
  currentPage = Number(meta.page ?? currentPage);
  pageSize = Number(meta.page_size ?? pageSize);
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  pagination.hidden = total <= pageSize;
  pageLabel.textContent = `第 ${currentPage} / ${totalPages} 页`;
  previousPage.disabled = currentPage <= 1;
  nextPage.disabled = currentPage >= totalPages;

  if (pollTimer) window.clearTimeout(pollTimer);
  pollTimer = hasActiveDocument
    ? window.setTimeout(() => loadDocuments().catch(showError), 3000)
    : null;
}

async function loadSummary() {
  const data = await requestJson("/api/knowledge-base");
  summaryTitle.textContent = data.name;
  summaryDescription.textContent = data.description || "当前 TestAgent 固定长期知识库";
  summaryStatus.textContent = statusLabels[data.status] || data.status;
  summaryStatus.className = data.status === "READY" ? "ready" : "";
  summaryCount.textContent = data.document_count ?? "—";
  summaryProfile.textContent = data.retrieval_profile || "—";
}

async function loadDocuments() {
  const data = await requestJson(`/api/knowledge-base/documents?page=${currentPage}&page_size=${pageSize}`);
  renderDocuments(data.documents, data.meta || {});
}

function showError(error) {
  showNotice(error instanceof Error ? error.message : "操作失败。", "error");
}

fileInput.addEventListener("change", () => {
  const file = fileInput.files?.[0];
  fileLabel.textContent = file?.name || "选择文件";
  const extension = file?.name.split(".").pop()?.toLowerCase() || "";
  mediaConsent.hidden = !mediaExtensions.has(extension);
  if (mediaConsent.hidden) mediaConsentInput.checked = false;
});

uploadForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const file = fileInput.files?.[0];
  if (!file || busy) return;
  const extension = file.name.split(".").pop()?.toLowerCase() || "";
  const isMedia = mediaExtensions.has(extension);
  if (isMedia && !mediaConsentInput.checked) {
    showNotice("上传音视频前需要确认硅基流动解析授权。", "error");
    return;
  }
  busy = true;
  uploadButton.disabled = true;
  uploadButton.textContent = "上传中…";
  try {
    const headers = { "Content-Type": file.type || "application/octet-stream" };
    if (isMedia) headers["X-User-Media-Consent"] = "true";
    await requestJson(`/api/knowledge-base/documents?filename=${encodeURIComponent(file.name)}`, {
      method: "POST", headers, body: file,
    });
    showNotice("文件已上传，系统正在解析。");
    uploadForm.reset();
    fileLabel.textContent = "选择文件";
    mediaConsent.hidden = true;
    currentPage = 1;
    await Promise.all([loadSummary(), loadDocuments()]);
  } catch (error) {
    showError(error);
  } finally {
    busy = false;
    uploadButton.disabled = false;
    uploadButton.textContent = "上传并解析";
  }
});

documentList.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-action]");
  if (!button || busy) return;
  const documentId = button.dataset.documentId;
  const action = button.dataset.action;
  if (action === "delete" && !window.confirm(`确定删除长期文档“${button.dataset.filename}”吗？删除后所有对话都无法再检索它。`)) return;
  busy = true;
  button.disabled = true;
  try {
    await requestJson(
      action === "retry"
        ? `/api/knowledge-base/documents/${documentId}/retry`
        : `/api/knowledge-base/documents/${documentId}`,
      { method: action === "retry" ? "POST" : "DELETE" },
    );
    showNotice(action === "retry" ? "已提交重试。" : "已提交删除。", "success");
    await Promise.all([loadSummary(), loadDocuments()]);
  } catch (error) {
    showError(error);
  } finally {
    busy = false;
    button.disabled = false;
  }
});

refreshButton.addEventListener("click", () => Promise.all([loadSummary(), loadDocuments()]).catch(showError));
previousPage.addEventListener("click", () => { if (currentPage > 1) { currentPage -= 1; loadDocuments().catch(showError); } });
nextPage.addEventListener("click", () => { currentPage += 1; loadDocuments().catch(showError); });

Promise.all([loadSummary(), loadDocuments()]).catch(showError);
