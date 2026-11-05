const $ = (id) => document.getElementById(id);
let version = 0;
let selected = new Set();
let timer;
let busy = false;
const labels = { READY: "已可用", FAILED: "解析失败", DELETED: "已删除", DELETING: "删除中", DELETE_FAILED: "删除失败" };
function notice(message, error = false) { $("notice").hidden = false; $("notice").textContent = message; $("notice").className = error ? "error" : ""; }
async function api(url, options) {
  const response = await fetch(url, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail?.message || data.detail?.error_type || "操作失败，请重试。");
  return data;
}
async function run(action) {
  if (busy) return;
  busy = true;
  document.querySelectorAll("button").forEach((button) => button.disabled = true);
  try { await action(); } catch (error) { notice(error.message, true); }
  finally { busy = false; document.querySelectorAll("button").forEach((button) => button.disabled = false); }
}
async function documents() {
  clearTimeout(timer);
  try {
    const data = await api("/api/knowledge-base/documents?page=1&page_size=100");
    $("documents").replaceChildren();
    for (const item of data.documents) {
      const label = document.createElement("label");
      const check = document.createElement("input");
      check.type = "checkbox";
      check.disabled = item.status !== "READY" && !selected.has(item.document_id);
      check.checked = selected.has(item.document_id);
      check.addEventListener("change", () => {
        if (check.checked) selected.add(item.document_id);
        else { selected.delete(item.document_id); check.disabled = item.status !== "READY"; }
      });
      label.append(check, document.createTextNode(`${item.filename} · ${labels[item.status] || "处理中"}`));
      $("documents").append(label);
    }
    $("document-status").textContent = data.documents.length ? "勾选需要关联的文档，再保存档案。" : "暂无文档，可直接用文本建档。";
    if (data.documents.some((item) => !labels[item.status])) timer = setTimeout(documents, 3000);
  } catch (_) { $("document-status").textContent = "文档服务暂不可用；仍可使用文本维护企业档案。已有文档关联不会自动清除。"; }
}
$("profile-form").addEventListener("submit", (event) => {
  event.preventDefault();
  void run(async () => {
    const result = await api("/api/enterprise", { method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ expected_version: version, company_name: $("company-name").value,
        company_profile: $("company-profile").value, document_ids: [...selected], confirmed: $("profile-confirm").checked }) });
    version = result.profile.version;
    $("version").textContent = `第 ${version} 版 · 已确认`;
    $("profile-confirm").checked = false;
    notice("企业档案已保存。新任务会默认使用；已有任务仍使用原版本。");
  });
});
$("upload-form").addEventListener("submit", (event) => {
  event.preventDefault();
  void run(async () => {
    const file = $("document-file").files[0];
    if (!file) return;
    if (!/\.(pdf|docx|txt|md)$/i.test(file.name) || file.size > 100 * 1024 * 1024) throw new Error("请选择不超过 100 MB 的 PDF、DOCX、TXT 或 Markdown 文档。");
    const result = await api(`/api/knowledge-base/documents?filename=${encodeURIComponent(file.name)}`, {
      method: "POST", headers: { "Content-Type": file.type || "application/octet-stream" }, body: file });
    notice(result.status === "READY" ? "文档已可用，可勾选关联。" : "文档已接收，等待解析。尚未更新企业档案。");
    $("upload-form").reset();
    await documents();
  });
});
$("document-draft").addEventListener("click", () => void run(async () => {
  if (!selected.size) throw new Error("请先勾选已可用的文档。");
  if ($("company-profile").value.trim() && !window.confirm("将用所选文档片段替换当前未保存的文本草稿，已保存档案不受影响。确认？")) return;
  const data = await api("/api/enterprise/document-draft", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ document_ids: [...selected] }) });
  $("company-profile").value = data.draft;
  $("profile-confirm").checked = false;
  notice(data.message);
}));
$("refresh-documents").addEventListener("click", documents);
await run(async () => {
  const { profile } = await api("/api/enterprise");
  if (profile) {
    version = profile.version;
    $("version").textContent = `第 ${version} 版 · 已确认`;
    $("company-name").value = profile.company_name;
    $("company-profile").value = profile.company_profile;
    selected = new Set(profile.documents.map((item) => item.document_id));
  }
});
void documents();
