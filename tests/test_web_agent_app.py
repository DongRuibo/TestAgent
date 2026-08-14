"""网页 Agent API 和静态入口的离线测试。"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient

import web_agent.app as web_app
from web_agent.app import create_app
from web_agent.conversations import SQLiteConversationStore


KNOWLEDGE_BASE_IDS = {
    "pdf": "11d66b91-701a-4e33-9f66-877244fae693",
    "office": "21d66b91-701a-4e33-9f66-877244fae693",
    "image": "31d66b91-701a-4e33-9f66-877244fae693",
    "media": "41d66b91-701a-4e33-9f66-877244fae693",
}
DOCUMENT_IDS = {
    "pdf": "51d66b91-701a-4e33-9f66-877244fae693",
    "office": "61d66b91-701a-4e33-9f66-877244fae693",
    "image": "71d66b91-701a-4e33-9f66-877244fae693",
    "media": "81d66b91-701a-4e33-9f66-877244fae693",
}
JOB_IDS = {
    "pdf": "91d66b91-701a-4e33-9f66-877244fae693",
    "office": "a1d66b91-701a-4e33-9f66-877244fae693",
    "image": "b1d66b91-701a-4e33-9f66-877244fae693",
    "media": "c1d66b91-701a-4e33-9f66-877244fae693",
}


class FakeReceipt:
    def to_dict(self):
        return {
            "task_id": "00000000-0000-0000-0000-000000000001",
            "attempt_id": "00000000-0000-0000-0000-000000000002",
            "history_message_count": 0,
            "history_omitted_count": 0,
            "memory_policy": "disabled",
            "memory_reason": "test",
            "context_pack_id": None,
            "memory_item_count": 0,
            "tool_result_count": 0,
            "estimated_context_chars": 10,
        }


class FakeWebAgent:
    def __init__(
        self,
        trace,
        calls,
        *,
        error: str | None = None,
        artifacts=None,
        tool_results=None,
    ) -> None:
        self.trace = trace
        self.calls = calls
        self.error = error
        self.generation_ui_artifacts = list(artifacts or [])
        self.last_tool_results = list(tool_results or [])
        self.last_context_receipt = None

    def run(
        self,
        task: str,
        *,
        history=None,
        episode_id=None,
        generation_ui_event=None,
    ) -> str:
        if generation_ui_event is None:
            self.calls.append((task, history, episode_id))
        else:
            self.calls.append((task, history, episode_id, generation_ui_event))
            self.generation_ui_artifacts = []
        self.trace("Step 1: 调用模型")
        if self.error:
            raise RuntimeError(self.error)
        self.trace("Step 1: 模型给出最终回答")
        self.last_context_receipt = FakeReceipt()
        return "网页回答"


class FakeAttachmentGateway:
    def __init__(
        self,
        *,
        kind: str = "office",
        job_status: str = "SUCCEEDED",
        root=None,
    ) -> None:
        self.kind = kind
        self.job_status = job_status
        self.root = root or self
        if root is None:
            self.uploads = []
            self.deleted = []
            self.requested_kinds = []

    @property
    def knowledge_base_id(self):
        return KNOWLEDGE_BASE_IDS[self.kind]

    @property
    def document_id(self):
        return DOCUMENT_IDS[self.kind]

    @property
    def job_id(self):
        return JOB_IDS[self.kind]

    def for_kind(self, kind):
        self.root.requested_kinds.append(kind)
        return FakeAttachmentGateway(
            kind=kind,
            job_status=self.job_status,
            root=self.root,
        )

    def close(self) -> None:
        pass

    def upload_document(self, file, **kwargs):
        self.root.uploads.append((self.kind, file.read(), kwargs))
        return {
            "success": True,
            "document_id": self.document_id,
            "job_id": self.job_id,
            "status": "RECEIVED",
        }

    def get_job(self, job_id):
        assert job_id in JOB_IDS.values()
        return {
            "success": True,
            "data": {
                "status": self.job_status,
                "progress": 1 if self.job_status == "SUCCEEDED" else 0.5,
            },
        }

    def get_document(self, document_id):
        assert document_id in DOCUMENT_IDS.values()
        return {"success": True, "data": {"status": "READY", "progress": 1}}

    def delete_document(self, document_id, *, attachment_id):
        self.root.deleted.append((document_id, attachment_id))
        return {"success": True, "job_id": "c1d66b91-701a-4e33-9f66-877244fae693"}


def _client(
    tmp_path: Path,
    *,
    error: str | None = None,
    gateway: FakeAttachmentGateway | None = None,
    scopes: list[
        tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]
    ] | None = None,
    artifacts=None,
    tool_results=None,
):
    calls = []

    def factory(trace, attachment_scope):
        if scopes is not None:
            scopes.append(
                (
                    attachment_scope.knowledge_base_ids,
                    attachment_scope.document_ids,
                    attachment_scope.filenames,
                )
            )
        return FakeWebAgent(
            trace,
            calls,
            error=error,
            artifacts=artifacts,
            tool_results=tool_results,
        )

    store = SQLiteConversationStore(tmp_path / "conversations.sqlite3")
    app = create_app(
        factory,
        conversation_store=store,
        attachment_gateway_factory=(
            lambda kind: gateway.for_kind(kind)
        ) if gateway is not None else None,
    )
    return TestClient(app), calls


def test_web_agent_serves_chat_page_and_static_assets(tmp_path) -> None:
    client, _calls = _client(tmp_path)

    page = client.get("/")
    styles = client.get("/static/app.css")
    generation_ui_script = client.get("/static/generation-ui/generation-ui.js")

    assert page.status_code == 200
    assert 'id="chat-title"' in page.text
    assert "LIVE DEMO" not in page.text
    assert "attachment-input" in page.text
    assert "generation-ui.css?v=0.5.2-upload1" in page.text
    assert "app.css?v=20260814-9" in page.text
    assert "app.js?v=20260814-8" in page.text
    assert styles.status_code == 200
    assert "--accent" in styles.text
    assert generation_ui_script.status_code == 200
    assert "GenerationUiRenderer" in generation_ui_script.text
    assert "patchForm" in generation_ui_script.text
    assert '"max-fraction-digits"' in generation_ui_script.text
    assert "e.node.props.step" in generation_ui_script.text
    assert "返回对话" in generation_ui_script.text
    assert "请填写必填项" in generation_ui_script.text
    assert "将任务状态更新为" in generation_ui_script.text

    app_script = client.get("/static/app.js")
    assert app_script.status_code == 200
    assert 'generation-ui.js?v=0.5.2-ux1' in app_script.text
    assert 'fetch(\n    `/api/content-assets/images?filename=' in app_script.text
    assert 'button.textContent = "选择/上传图片"' in app_script.text
    assert "picker.multiple = true" in app_script.text
    assert "instance.patchForm({ image_paths_text:" in app_script.text


def test_web_agent_keeps_shell_fixed_and_clears_input_before_request(tmp_path) -> None:
    client, _calls = _client(tmp_path)

    styles = client.get("/static/app.css").text
    script = client.get("/static/app.js").text

    assert "html, body { margin: 0; height: 100%; min-height: 0; overflow: hidden" in styles
    assert "grid-template-rows: auto minmax(0, 1fr) auto" in styles
    assert ".messages { min-height: 0; overflow-y: auto" in styles
    send_start = script.index("async function sendMessage")
    clear_input = script.index('input.value = "";', send_start)
    request_start = script.index("await fetch", send_start)
    assert clear_input < request_start


def test_web_agent_exposes_p0_mobile_and_conversation_controls(tmp_path) -> None:
    client, _calls = _client(tmp_path)

    page = client.get("/").text
    styles = client.get("/static/app.css").text
    script = client.get("/static/app.js").text

    assert 'id="mobile-menu-button"' in page
    assert 'id="new-chat"' in page
    assert 'id="delete-chat"' in page
    assert 'id="retry-restore"' in page
    assert "body.sidebar-open .sidebar" in styles
    assert "body.sidebar-open .sidebar-backdrop" in styles
    assert 'event.isComposing || event.keyCode === 229' in script
    assert 'configurationComplete ? "配置完整" : "配置待补充"' in script
    assert "Agent 就绪" not in script
    assert 'retryButton.textContent = "重新执行"' in script
    assert "error.retryable = streamError.retryable === true" in script
    assert ".chat-retry" in styles

    new_chat_start = script.index("function startNewConversation")
    delete_chat_start = script.index("async function deleteCurrentConversation")
    new_chat_source = script[new_chat_start:delete_chat_start]
    assert 'method: "DELETE"' not in new_chat_source
    assert "window.confirm" in script[delete_chat_start:]

    restore_start = script.index("async function loadConversation")
    restore_end = script.index("function saveConversationId", restore_start)
    restore_source = script[restore_start:restore_end]
    restore_failure = restore_source.split("} catch (_) {", maxsplit=1)[1]
    assert "response.status === 404" in restore_source
    assert "removeStoredConversationId()" not in restore_failure
    assert "retry: true" in restore_failure


def test_web_agent_exposes_history_sidebar_without_capabilities(tmp_path) -> None:
    client, _calls = _client(tmp_path)

    page = client.get("/").text
    styles = client.get("/static/app.css").text
    script = client.get("/static/app.js").text

    assert 'id="conversation-list"' in page
    assert 'id="history-empty"' in page
    assert 'id="history-search"' in page
    assert "历史对话" in page
    assert "＋ 新对话" in page
    assert "一次工具调用，一个临时沙箱" not in page
    assert '<details class="status-card"' in page
    assert '<details class="demo-tools">' in page
    assert "可用能力" not in page
    assert ".conversation-item.active" in styles
    assert "function updateChatTitle()" in script
    assert "chatTitle.title = title" in script
    assert 'fetch("/api/conversations?limit=50")' in script
    assert "async function switchConversation" in script
    assert "function conversationTimeGroup" in script
    assert "function openRenameConversation" in script
    assert "async function submitConversationRename" in script
    assert "window.prompt" not in script
    assert 'method: "PATCH"' in script
    assert ".dataset.renameConversation" in script
    assert ".dataset.deleteConversation" in script
    assert ".history-group" in styles
    assert ".conversation-item-menu" in styles
    assert 'id="conversation-rename-dialog"' in page


def test_web_agent_exposes_p1_p2_chat_controls_and_safe_markdown(tmp_path) -> None:
    client, _calls = _client(tmp_path)

    page = client.get("/").text
    styles = client.get("/static/app.css").text
    script = client.get("/static/app.js").text
    markdown = client.get("/static/markdown.js").text

    assert 'id="stop-button"' in page
    assert 'id="sidebar-collapse"' in page
    assert 'id="data-disclosure"' in page
    assert 'id="attachment-guidance"' in page
    assert 'fetch("/api/chat/stream"' in script
    assert "async function stopCurrentRequest" in script
    assert "renderMarkdown(bubble, content)" in script
    assert "window.alert" not in script
    assert "innerHTML" not in markdown
    assert '["http:", "https:"]' in markdown
    assert "javascript:" not in markdown
    assert "env(safe-area-inset-bottom)" in styles
    assert "body.sidebar-collapsed .app-shell" in styles
    assert "--genui-workspace: #091512" in styles
    assert (
        ".chat-header h2 { overflow: hidden; margin: 0; color: var(--text); "
        "font-size: 16px;"
    ) in styles
    assert ".p-datatable-tbody > tr" in styles
    assert "background: var(--genui-surface);" in styles
    assert ".genui-header > div > span:first-child" in styles
    assert ".genui-tools," in styles
    assert ".genui-mode { display: none; }" in styles
    assert ".p-select-overlay" in styles
    assert ".resource-card" in styles
    assert "--focus: #7ea7ff" in styles
    assert ".generation-ui-cancel-button" in styles
    assert ".generation-ui-validation-hint" in styles
    assert 'data-scenario="structured_input"' in styles
    assert "服务异常" in script


def test_web_agent_compacts_chat_output_and_transient_new_chat_notice(tmp_path) -> None:
    client, _calls = _client(tmp_path)

    styles = client.get("/static/app.css").text
    script = client.get("/static/app.js").text

    assert 'row.classList.add("answer")' in script
    assert ".message-row.answer .bubble" in styles
    assert 'status.className = "process-status"' in script
    assert 'bubble.className = "bubble process-answer"' in script
    assert "makeStreamingAnswer(process)" in script
    assert "resetStreamingAnswer(streamedAnswer)" in script
    assert "makeStreamingAnswer()" not in script
    assert "container.textContent = label" in script
    assert "process-list" not in styles
    assert ".process-answer" in styles
    assert 'const section = document.createElement("details")' in script
    assert 'const heading = document.createElement("summary")' in script
    assert "产物与来源 · ${resources.length} 项" in script
    assert "window.setTimeout(hideNotice, autoHideMs)" in script

    new_chat_start = script.index("function startNewConversation")
    delete_chat_start = script.index("async function deleteCurrentConversation")
    new_chat_source = script[new_chat_start:delete_chat_start]
    assert "autoHideMs: 3000" in new_chat_source


def test_web_agent_marks_legacy_execution_details_and_keeps_cancelled_turn(
    tmp_path,
) -> None:
    client, _calls = _client(tmp_path)

    styles = client.get("/static/app.css").text
    script = client.get("/static/app.js").text

    assert 'executionDetailsUnavailable: item.role === "assistant"' in script
    assert "该回答生成于升级前，未保存执行详情。" in script
    assert ".execution-details-unavailable" in styles

    send_start = script.index("async function sendMessage")
    resize_start = script.index("function resizeInput", send_start)
    send_source = script[send_start:resize_start]
    assert 'makeMessage("user", message)' in send_source
    assert "userRow.remove()" not in send_source
    assert 'event.type === "answer_delta"' in send_source
    assert "finishStreamingAnswer" in send_source
    assert send_source.count("autoHideMs: 4000") == 2
    assert "问题和已生成的内容已保留" in send_source


def test_web_agent_lists_conversation_summaries_by_recent_activity(tmp_path) -> None:
    client, _calls = _client(tmp_path)
    empty_id = client.post("/api/conversations").json()["conversation_id"]
    active = client.post("/api/chat", json={"message": "  分析   本月经营数据  "})
    active_id = active.json()["conversation_id"]

    response = client.get("/api/conversations?limit=50")

    assert response.status_code == 200
    summaries = response.json()["conversations"]
    assert len(summaries) == 2
    assert summaries[0]["conversation_id"] == active_id
    assert summaries[0]["title"] == "分析 本月经营数据"
    assert summaries[0]["message_count"] == 2
    assert summaries[0]["updated_at"]
    assert summaries[1]["conversation_id"] == empty_id
    assert summaries[1]["title"] == "新对话"
    assert summaries[1]["message_count"] == 0
    assert summaries[1]["updated_at"]


def test_web_agent_validates_conversation_summary_limit(tmp_path) -> None:
    client, _calls = _client(tmp_path)

    assert client.get("/api/conversations?limit=0").status_code == 422
    assert client.get("/api/conversations?limit=101").status_code == 422


def test_web_agent_renames_conversation_and_validates_title(tmp_path) -> None:
    client, _calls = _client(tmp_path)
    created = client.post("/api/chat", json={"message": "原始问题"}).json()
    conversation_id = created["conversation_id"]

    renamed = client.patch(
        f"/api/conversations/{conversation_id}",
        json={"title": "  每周经营复盘  "},
    )

    assert renamed.status_code == 200
    assert renamed.json() == {
        "conversation_id": conversation_id,
        "title": "每周经营复盘",
    }
    summaries = client.get("/api/conversations").json()["conversations"]
    assert summaries[0]["title"] == "每周经营复盘"
    assert client.patch(
        f"/api/conversations/{conversation_id}",
        json={"title": "   "},
    ).status_code == 422
    assert client.patch(
        "/api/conversations/00000000-0000-0000-0000-000000000000",
        json={"title": "不存在"},
    ).status_code == 404


def test_web_agent_persists_history_and_restores_it_by_conversation_id(tmp_path) -> None:
    client, calls = _client(tmp_path)

    first = client.post("/api/chat", json={"message": "你好"})
    first_payload = first.json()
    conversation_id = first_payload["conversation_id"]
    response = client.post(
        "/api/chat",
        json={"message": "继续", "conversation_id": conversation_id},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["answer"] == "网页回答"
    assert payload["trace"] == [
        "Step 1: 调用模型",
        "Step 1: 模型给出最终回答",
    ]
    assert isinstance(payload["duration_ms"], int)
    assert payload["conversation_id"] == conversation_id
    assert payload["context"]["estimated_context_chars"] == 10
    assert calls == [
        ("你好", [], conversation_id),
        (
            "继续",
            [
                {"role": "user", "content": "你好"},
                {"role": "assistant", "content": "网页回答"},
            ],
            conversation_id,
        )
    ]
    restored = client.get(f"/api/conversations/{conversation_id}")
    assert restored.status_code == 200
    restored_messages = restored.json()["messages"]
    assert restored_messages[0] == {"role": "user", "content": "你好"}
    assert restored_messages[2] == {"role": "user", "content": "继续"}
    for assistant_message, original in (
        (restored_messages[1], first_payload),
        (restored_messages[3], payload),
    ):
        assert assistant_message["role"] == "assistant"
        assert assistant_message["content"] == "网页回答"
        assert assistant_message["trace"] == original["trace"]
        assert assistant_message["duration_ms"] == original["duration_ms"]
        assert assistant_message["context"] == original["context"]

    script = client.get("/static/app.js").text
    load_start = script.index("async function loadConversation")
    load_end = script.index("function saveConversationId", load_start)
    load_source = script[load_start:load_end]
    assert "trace: item.trace" in load_source
    assert "duration: item.duration_ms" in load_source
    assert "context: item.context" in load_source


def test_web_agent_streams_status_and_result_without_breaking_json_api(
    tmp_path,
) -> None:
    client, _calls = _client(tmp_path)

    response = client.post(
        "/api/chat/stream",
        json={"request_id": str(uuid4()), "message": "你好"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    events = [json.loads(line) for line in response.text.splitlines()]
    assert events[0]["type"] == "accepted"
    assert [item["message"] for item in events if item["type"] == "status"] == [
        "Step 1: 调用模型",
        "Step 1: 模型给出最终回答",
    ]
    assert events[-1]["type"] == "result"
    assert events[-1]["data"]["answer"] == "网页回答"
    assert events[-1]["data"]["conversation_id"]


def test_web_agent_streams_answer_before_final_result(tmp_path) -> None:
    class StreamingAgent:
        generation_ui_artifacts = []
        last_tool_results = []
        last_context_receipt = FakeReceipt()

        def run(
            self,
            task,
            *,
            history=None,
            episode_id=None,
            on_answer_delta=None,
            on_answer_reset=None,
            is_cancelled=None,
        ):
            del task, history, episode_id, on_answer_reset
            assert not is_cancelled()
            on_answer_delta("流式")
            on_answer_delta("回答")
            return "流式回答"

    app = create_app(
        lambda _trace, _scope: StreamingAgent(),
        conversation_store=SQLiteConversationStore(
            tmp_path / "conversations.sqlite3"
        ),
    )
    response = TestClient(app).post(
        "/api/chat/stream",
        json={"request_id": str(uuid4()), "message": "你好"},
    )

    events = [json.loads(line) for line in response.text.splitlines()]
    assert [item["type"] for item in events] == [
        "accepted",
        "answer_delta",
        "answer_delta",
        "result",
    ]
    assert "".join(
        item["delta"] for item in events if item["type"] == "answer_delta"
    ) == events[-1]["data"]["answer"]


def test_web_agent_cooperatively_cancels_stream_and_persists_user_turn(
    tmp_path,
) -> None:
    started = threading.Event()
    release = threading.Event()

    class BlockingAgent:
        generation_ui_artifacts = []
        last_tool_results = []
        last_context_receipt = None

        def __init__(self, trace) -> None:
            self.trace = trace

        def run(self, task, *, history=None, episode_id=None):
            self.trace("Step 1: 调用模型")
            started.set()
            assert release.wait(timeout=2)
            self.trace("Step 1: 模型给出最终回答")
            return "不应保存"

    app = create_app(
        lambda trace, _scope: BlockingAgent(trace),
        conversation_store=SQLiteConversationStore(
            tmp_path / "conversations.sqlite3"
        ),
    )
    client = TestClient(app)
    request_id = str(uuid4())
    result: dict[str, object] = {}

    def send_request() -> None:
        result["response"] = client.post(
            "/api/chat/stream",
            json={"request_id": request_id, "message": "请执行"},
        )

    thread = threading.Thread(target=send_request)
    thread.start()
    assert started.wait(timeout=2)

    cancelled = client.delete(f"/api/chat/stream/{request_id}")
    release.set()
    thread.join(timeout=3)

    assert cancelled.status_code == 202
    assert cancelled.json() == {"status": "cancellation_requested"}
    assert not thread.is_alive()
    response = result["response"]
    assert isinstance(response, type(cancelled))
    events = [json.loads(line) for line in response.text.splitlines()]
    assert events[-1]["type"] == "cancelled"
    assert events[-1]["message"] == "已停止生成。"
    conversation_id = events[-1]["conversation_id"]
    assert conversation_id
    restored = client.get(f"/api/conversations/{conversation_id}").json()
    assert restored["messages"] == [
        {"role": "user", "content": "请执行", "status": "stopped"}
    ]
    assert client.get("/api/conversations").json()["conversations"][0][
        "conversation_id"
    ] == conversation_id


def test_web_agent_streams_answer_deltas_and_persists_partial_answer_on_stop(
    tmp_path,
) -> None:
    first_delta = threading.Event()
    release = threading.Event()

    class StreamingAgent:
        generation_ui_artifacts = []
        last_tool_results = []
        last_context_receipt = None

        def run(
            self,
            task,
            *,
            history=None,
            episode_id=None,
            on_answer_delta=None,
            on_answer_reset=None,
            is_cancelled=None,
        ):
            del task, history, episode_id, on_answer_reset
            on_answer_delta("第一段")
            first_delta.set()
            assert release.wait(timeout=2)
            if is_cancelled():
                raise web_app.LLMStreamCancelled("cancelled")
            on_answer_delta("第二段")
            return "第一段第二段"

    store = SQLiteConversationStore(tmp_path / "conversations.sqlite3")
    app = create_app(
        lambda _trace, _scope: StreamingAgent(),
        conversation_store=store,
    )
    client = TestClient(app)
    request_id = str(uuid4())
    result: dict[str, object] = {}

    def send_request() -> None:
        result["response"] = client.post(
            "/api/chat/stream",
            json={"request_id": request_id, "message": "保留这个问题"},
        )

    thread = threading.Thread(target=send_request)
    thread.start()
    assert first_delta.wait(timeout=2)
    assert client.delete(f"/api/chat/stream/{request_id}").status_code == 202
    release.set()
    thread.join(timeout=3)

    response = result["response"]
    events = [json.loads(line) for line in response.text.splitlines()]
    assert [item for item in events if item["type"] == "answer_delta"] == [
        {"type": "answer_delta", "delta": "第一段"}
    ]
    assert events[-1]["type"] == "cancelled"
    conversation_id = events[0]["conversation_id"]
    restored = client.get(f"/api/conversations/{conversation_id}").json()
    assert restored["messages"][0] == {
        "role": "user",
        "content": "保留这个问题",
        "status": "stopped",
    }
    assert restored["messages"][1]["role"] == "assistant"
    assert restored["messages"][1]["content"] == "第一段"
    assert restored["messages"][1]["status"] == "stopped"
    assert isinstance(restored["messages"][1]["duration_ms"], int)
    assert store.messages(conversation_id) == []


def test_web_agent_persists_safe_tool_resources_and_downloads_artifact(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(web_app, "PROJECT_ROOT", tmp_path)
    artifact = tmp_path / "artifacts" / "report.txt"
    artifact.parent.mkdir()
    artifact.write_text("synthetic report", encoding="utf-8")
    tool_results = [
        {
            "tool_name": "web_search",
            "result": {
                "success": True,
                "results": [
                    {
                        "title": "官方说明",
                        "url": "https://example.com/docs",
                        "snippet": "公开网页摘要",
                    }
                ],
            },
        },
        {
            "tool_name": "search_knowledge_base",
            "result": {
                "success": True,
                "candidates": [
                    {
                        "chunk_id": "chunk-1",
                        "document_name": "合同.pdf",
                        "pages": [3],
                        "content": "付款节点原文",
                    }
                ],
            },
        },
        {
            "tool_name": "run_code",
            "result": {
                "success": True,
                "artifacts": [
                    {
                        "path": "artifacts/report.txt",
                        "media_type": "text/plain",
                    },
                    {"path": "artifacts/../secret.txt"},
                ],
            },
        },
    ]
    client, _calls = _client(tmp_path, tool_results=tool_results)

    response = client.post("/api/chat", json={"message": "给我证据"})

    assert response.status_code == 200
    resources = response.json()["resources"]
    assert [item["kind"] for item in resources] == [
        "source",
        "evidence",
        "artifact",
    ]
    assert resources[0]["href"] == "https://example.com/docs"
    assert resources[1]["title"] == "合同.pdf · 第 3 页"
    artifact_resource = resources[2]
    downloaded = client.get(artifact_resource["href"])
    assert downloaded.status_code == 200
    assert downloaded.content == b"synthetic report"
    conversation_id = response.json()["conversation_id"]
    restored = client.get(f"/api/conversations/{conversation_id}").json()
    assert restored["messages"][1]["resources"] == resources


def test_web_agent_returns_and_restores_generation_ui_artifact(tmp_path) -> None:
    artifact_id = "e1d66b91-701a-4e33-9f66-877244fae693"
    artifact = {
        "type": "generation_ui",
        "artifact_id": artifact_id,
        "scenario": "sales_briefing",
        "text": "销售额增长 12%。",
        "spec": {
            "version": "0.1",
            "scenario": "sales_briefing",
            "interaction_mode": "read_only",
            "title": "本月经营简报",
            "summary": "销售额增长 12%。",
            "root": {"id": "root", "type": "Column", "props": {}, "children": []},
            "actions": [],
            "form_defaults": {},
        },
        "data": {"metrics": {"sales": 1_280_000}},
    }
    client, calls = _client(tmp_path, artifacts=[artifact])

    response = client.post("/api/chat", json={"message": "展示销售简报"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["artifacts"][0]["artifact_id"] == artifact_id
    assert payload["artifacts"][0]["type"] == "generation_ui"
    conversation_id = payload["conversation_id"]
    restored = client.get(f"/api/conversations/{conversation_id}").json()
    assert "artifacts" not in restored["messages"][0]
    assert restored["messages"][1]["artifacts"][0]["artifact_id"] == artifact_id


def test_default_web_agent_wires_analysis_result_ref_to_generation_ui(
    monkeypatch,
) -> None:
    monkeypatch.setitem(
        web_app.DEFAULT_TOOL_HANDLERS,
        "analyze_spreadsheet",
        lambda path: {"success": True, "analysis": {"source_file": path}},
    )
    monkeypatch.setattr(
        web_app.LLMClient,
        "from_env",
        classmethod(lambda cls: object()),
    )
    monkeypatch.setattr(web_app, "build_memory_client_from_env", lambda: None)
    monkeypatch.setattr(web_app, "build_lazytt_mcp_tools_from_env", lambda: None)
    agent = web_app._default_agent_factory(
        lambda _message: None,
        web_app.AttachmentRetrievalScope(),
    )

    analysis = agent.tool_handlers["analyze_spreadsheet"]("fixtures/sales.csv")
    result = agent.tool_handlers["present_generation_ui"](
        "sales_briefing",
        "销售经营简报",
        analysis["presentation"]["result_ref"],
    )

    assert result["success"] is True
    assert len(agent.generation_ui_artifacts) == 1
    assert "order_count" not in agent.generation_ui_artifacts[0]["data"]["metrics"]

    draft = agent.tool_handlers["prepare_renovation_project"](
        "杭州市", 120, "现代简约"
    )
    project_ui = agent.tool_handlers["present_generation_ui"](
        "renovation_project_create",
        "创建装修项目",
        draft["presentation"]["result_ref"],
    )
    assert project_ui["success"] is True
    assert agent.generation_ui_artifacts[1]["spec"]["interaction_mode"] == "host_action"
    assert {
        "prepare_renovation_project",
        "calculate_material_quantities",
        "compare_renovation_quotes",
        "plan_construction_schedule",
        "review_renovation_contract",
        "prepare_generation_ui_input",
    } <= set(agent.tool_handlers)


def test_default_web_agent_auto_presents_complete_owner_briefing(
    monkeypatch, tmp_path
) -> None:
    from tools.owner_briefing import (
        OWNER_BRIEFING_TOOL_NAME,
        generate_owner_daily_briefing,
    )

    monkeypatch.setitem(
        web_app.DEFAULT_TOOL_HANDLERS,
        OWNER_BRIEFING_TOOL_NAME,
        lambda report_date: generate_owner_daily_briefing(
            report_date, output_dir=tmp_path / "artifacts"
        ),
    )
    monkeypatch.setattr(
        web_app.LLMClient,
        "from_env",
        classmethod(lambda cls: object()),
    )
    monkeypatch.setattr(web_app, "build_memory_client_from_env", lambda: None)
    monkeypatch.setattr(web_app, "build_lazytt_mcp_tools_from_env", lambda: None)
    agent = web_app._default_agent_factory(
        lambda _message: None,
        web_app.AttachmentRetrievalScope(),
    )

    result = agent.tool_handlers[OWNER_BRIEFING_TOOL_NAME]("2026-08-13")

    assert result["compact_result_complete"] is True
    assert result["ui_presented"] is True
    assert "presentation" not in result
    assert len(result["briefing"]["anomalies"]) == 6
    assert len(agent.generation_ui_artifacts) == 1
    assert agent.generation_ui_artifacts[0]["scenario"] == "owner_daily_briefing"
    assert len(agent.generation_ui_artifacts[0]["data"]["anomalies"]) == 6


def test_default_web_agent_merges_namespaced_mcp_tool(monkeypatch) -> None:
    tool_name = "mcp__lazytt__area_calculate"
    definition = {
        "type": "function",
        "function": {
            "name": tool_name,
            "description": "远程面积计算。",
            "parameters": {
                "type": "object",
                "properties": {"rooms": {"type": "array"}},
                "required": ["rooms"],
                "additionalProperties": False,
            },
        },
    }
    monkeypatch.setattr(
        web_app.LLMClient,
        "from_env",
        classmethod(lambda cls: object()),
    )
    monkeypatch.setattr(web_app, "build_memory_client_from_env", lambda: None)
    monkeypatch.setattr(
        web_app,
        "build_lazytt_mcp_tools_from_env",
        lambda: SimpleNamespace(
            tool_definitions={tool_name: definition},
            tool_handlers={tool_name: lambda rooms: {"success": True}},
            tool_budgets={tool_name: 1},
        ),
    )

    agent = web_app._default_agent_factory(
        lambda _message: None,
        web_app.AttachmentRetrievalScope(),
    )

    assert tool_name in agent.tool_handlers
    assert "calculate_material_quantities" not in agent.tool_handlers
    assert agent.tool_budgets[tool_name] == 1
    assert any(tool["function"]["name"] == tool_name for tool in agent.tools)
    assert not any(
        tool["function"]["name"] == "calculate_material_quantities"
        for tool in agent.tools
    )


def test_default_web_agent_wires_fixed_material_input_for_complete_mcp_set(
    monkeypatch,
) -> None:
    tool_names = [
        "mcp__lazytt__paint_calculate",
        "mcp__lazytt__tile_calculate",
        "mcp__lazytt__flooring_calculate",
    ]
    definitions = {
        name: {
            "type": "function",
            "function": {
                "name": name,
                "description": "远程材料计算。",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            },
        }
        for name in tool_names
    }
    monkeypatch.setattr(
        web_app.LLMClient,
        "from_env",
        classmethod(lambda cls: object()),
    )
    monkeypatch.setattr(web_app, "build_memory_client_from_env", lambda: None)
    agent = web_app._default_agent_factory(
        lambda _message: None,
        web_app.AttachmentRetrievalScope(),
        mcp_tools=SimpleNamespace(
            tool_definitions=definitions,
            tool_handlers={name: lambda: {"success": False} for name in tool_names},
            tool_budgets={name: 1 for name in tool_names},
        ),
    )

    assert "prepare_material_estimate_input" in agent.tool_handlers
    assert any(
        tool["function"]["name"] == "prepare_material_estimate_input"
        for tool in agent.tools
    )
    prepared = agent.tool_handlers["prepare_material_estimate_input"]()
    rendered = agent.tool_handlers["present_generation_ui"](
        prepared["presentation"]["scenario"],
        "材料采购估算",
        prepared["presentation"]["result_ref"],
    )
    assert rendered["success"] is True
    assert agent.generation_ui_artifacts[0]["scenario"] == "structured_input"


def test_default_web_agent_maps_mcp_material_result_to_generation_ui(
    monkeypatch,
) -> None:
    tool_name = "mcp__lazytt__paint_calculate"
    monkeypatch.setattr(
        web_app.LLMClient,
        "from_env",
        classmethod(lambda cls: object()),
    )
    monkeypatch.setattr(web_app, "build_memory_client_from_env", lambda: None)
    mcp_tools = SimpleNamespace(
        tool_definitions={
            tool_name: {
                "type": "function",
                "function": {
                    "name": tool_name,
                    "description": "远程油漆计算。",
                    "parameters": {
                        "type": "object",
                        "properties": {"paintAreaM2": {"type": "number"}},
                        "required": ["paintAreaM2"],
                        "additionalProperties": False,
                    },
                },
            }
        },
        tool_handlers={
            tool_name: lambda **_arguments: {
                "success": True,
                "source": "mcp",
                "server": "lazytt",
                "tool": "paint_calculate",
                "result": {
                    "summary": "油漆已计算。",
                    "primer": {
                        "packages": [{"sizeL": 5, "quantity": 1}],
                        "purchasedLiters": 5,
                        "surplusLiters": 0.5,
                        "theoreticalLiters": 4,
                        "withWasteLiters": 4.5,
                    },
                    "finish": {
                        "packages": [{"sizeL": 5, "quantity": 2}],
                        "purchasedLiters": 10,
                        "surplusLiters": 1,
                        "theoreticalLiters": 8,
                        "withWasteLiters": 9,
                    },
                },
            }
        },
        tool_budgets={tool_name: 1},
    )
    agent = web_app._default_agent_factory(
        lambda _message: None,
        web_app.AttachmentRetrievalScope(),
        mcp_tools=mcp_tools,
    )

    result = agent.tool_handlers[tool_name](
        paintAreaM2=50,
        primerCoats=1,
        finishCoats=2,
        wasteRate=0.1,
    )
    presentation = result["presentation"]
    rendered = agent.tool_handlers["present_generation_ui"](
        presentation["scenario"],
        "油漆采购估算",
        presentation["result_ref"],
    )

    assert rendered["success"] is True
    assert agent.generation_ui_artifacts[0]["scenario"] == "mcp_material_estimate"


def test_web_mcp_cache_discovers_tools_only_once(monkeypatch) -> None:
    config = web_app.LazyTtMcpConfig(
        server_name="lazytt",
        server_url="http://127.0.0.1:8080/mcp",
        token_url="http://127.0.0.1:8090/token",
        client_id="testagent",
        client_secret="test-secret",
    )
    tools = SimpleNamespace(
        tool_definitions={"mcp__lazytt__area_calculate": {}},
        tool_handlers={},
        tool_budgets={},
    )
    discoveries: list[object] = []
    monkeypatch.setattr(
        web_app.LazyTtMcpConfig,
        "from_env",
        classmethod(lambda cls: config),
    )
    monkeypatch.setattr(
        web_app.LazyTtMcpTools,
        "from_config",
        classmethod(lambda cls, value: discoveries.append(value) or tools),
    )
    cache = web_app._LazyTtMcpToolCache()

    assert cache.get() is tools
    assert cache.get() is tools
    assert discoveries == [config]
    assert cache.status() == {
        "enabled": True,
        "configured": True,
        "connected": True,
        "tools": ["mcp__lazytt__area_calculate"],
    }


def test_web_mcp_cache_retries_after_discovery_failure(monkeypatch) -> None:
    config = web_app.LazyTtMcpConfig(
        server_name="lazytt",
        server_url="http://127.0.0.1:8080/mcp",
        token_url="http://127.0.0.1:8090/token",
        client_id="testagent",
        client_secret="test-secret",
    )
    tools = SimpleNamespace(
        tool_definitions={"mcp__lazytt__area_calculate": {}},
        tool_handlers={},
        tool_budgets={},
    )
    attempts = iter([web_app.LazyTtMcpError("unavailable"), tools])
    monkeypatch.setattr(
        web_app.LazyTtMcpConfig,
        "from_env",
        classmethod(lambda cls: config),
    )

    def discover(_cls, _config):
        result = next(attempts)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(
        web_app.LazyTtMcpTools,
        "from_config",
        classmethod(discover),
    )
    cache = web_app._LazyTtMcpToolCache()

    assert cache.status()["connected"] is False
    assert cache.get() is tools


def test_project_action_is_scoped_idempotent_and_persisted(tmp_path) -> None:
    artifact_id = "d1d66b91-701a-4e33-9f66-877244fae693"
    artifact = {
        "type": "generation_ui",
        "artifact_id": artifact_id,
        "scenario": "renovation_project_create",
        "text": "请确认地址。",
        "spec": {
            "version": "0.1",
            "scenario": "renovation_project_create",
            "interaction_mode": "host_action",
            "title": "创建装修项目",
            "summary": "请确认地址。",
            "root": {"id": "root", "type": "Column", "props": {}, "children": []},
            "actions": [
                {
                    "id": "confirm_create_project",
                    "label": "确认创建项目",
                    "tool": "untrusted-browser-tool",
                    "confirm": True,
                    "tone": "primary",
                }
            ],
            "form_defaults": {"address": ""},
        },
        "data": {
            "city": "杭州市",
            "area_m2": 120,
            "style": "现代简约",
            "status": "待确认地址",
        },
    }
    client, calls = _client(tmp_path, artifacts=[artifact])
    chat = client.post("/api/chat", json={"message": "创建装修项目"}).json()
    stored = chat["artifacts"][0]
    idempotency_key = "e1d66b91-701a-4e33-9f66-877244fae693"
    request = {
        "conversation_id": chat["conversation_id"],
        "message_id": int(stored["message_id"]),
        "artifact_id": artifact_id,
        "scenario": "renovation_project_create",
        "action_id": "confirm_create_project",
        "arguments": {"address": "杭州市西湖区建设路 88 号"},
        "idempotency_key": idempotency_key,
    }

    first = client.post("/api/generation/action", json=request)
    repeated = client.post("/api/generation/action", json=request)
    changed_same_key = client.post(
        "/api/generation/action",
        json={
            **request,
            "arguments": {"address": "杭州市西湖区另一条路 1 号"},
        },
    )

    assert first.status_code == 200
    assert repeated.status_code == 200
    assert changed_same_key.status_code == 409
    assert first.json() == repeated.json()
    assert first.json()["data_patch"]["status"] == "已创建"
    restored = client.get(f"/api/conversations/{chat['conversation_id']}").json()
    restored_artifact = restored["messages"][1]["artifacts"][0]
    assert restored_artifact["data"]["address"] == "杭州市西湖区建设路 88 号"
    assert restored_artifact["data"]["project_id"] == first.json()["data_patch"]["project_id"]


def test_project_action_rejects_cross_conversation_and_wrong_city(tmp_path) -> None:
    artifact_id = "f1d66b91-701a-4e33-9f66-877244fae693"
    artifact = {
        "type": "generation_ui",
        "artifact_id": artifact_id,
        "scenario": "renovation_project_create",
        "text": "请确认地址。",
        "spec": {
            "version": "0.1",
            "scenario": "renovation_project_create",
            "interaction_mode": "host_action",
            "title": "创建装修项目",
            "summary": "请确认地址。",
            "root": {"id": "root", "type": "Column", "props": {}, "children": []},
            "actions": [
                {
                    "id": "confirm_create_project",
                    "label": "确认创建项目",
                    "confirm": True,
                }
            ],
            "form_defaults": {"address": ""},
        },
        "data": {"city": "杭州市", "area_m2": 120, "style": "现代简约", "status": "待确认地址"},
    }
    client, _calls = _client(tmp_path, artifacts=[artifact])
    chat = client.post("/api/chat", json={"message": "创建装修项目"}).json()
    other_conversation = client.post("/api/conversations").json()["conversation_id"]
    base = {
        "conversation_id": chat["conversation_id"],
        "message_id": int(chat["artifacts"][0]["message_id"]),
        "artifact_id": artifact_id,
        "scenario": "renovation_project_create",
        "action_id": "confirm_create_project",
        "arguments": {"address": "上海市浦东新区测试路 1 号"},
        "idempotency_key": "a2d66b91-701a-4e33-9f66-877244fae693",
    }

    wrong_city = client.post("/api/generation/action", json=base)
    cross_conversation = client.post(
        "/api/generation/action",
        json={
            **base,
            "conversation_id": other_conversation,
            "arguments": {"address": "杭州市西湖区测试路 1 号"},
            "idempotency_key": "b2d66b91-701a-4e33-9f66-877244fae693",
        },
    )

    assert wrong_city.status_code == 422
    assert wrong_city.json()["detail"]["error_type"] == "PROJECT_ADDRESS_CITY_MISMATCH"
    assert cross_conversation.status_code == 404


def test_structured_input_event_validates_persists_and_returns_to_agent(tmp_path) -> None:
    artifact_id = "f2d66b91-701a-4e33-9f66-877244fae693"
    argument_schema = {
        "type": "object",
        "properties": {
            "departure": {"type": "string", "maxLength": 500},
            "travel_date": {"type": "string", "format": "date", "maxLength": 500},
            "cabin": {
                "type": "string",
                "enum": ["经济舱", "公务舱"],
                "maxLength": 500,
            },
        },
        "required": ["departure", "travel_date", "cabin"],
        "additionalProperties": False,
    }
    artifact = {
        "type": "generation_ui",
        "artifact_id": artifact_id,
        "scenario": "structured_input",
        "text": "请填写行程条件。",
        "spec": {
            "version": "0.1",
            "scenario": "structured_input",
            "interaction_mode": "host_action",
            "title": "行程条件",
            "summary": "请填写行程条件。",
            "root": {"id": "root", "type": "Column", "props": {}, "children": []},
            "actions": [
                {
                    "id": "submit_structured_input",
                    "label": "提交并继续",
                    "event_type": "submit",
                    "confirm": False,
                    "arguments_schema": argument_schema,
                }
            ],
            "form_defaults": {
                "departure": "",
                "travel_date": "",
                "cabin": "",
            },
        },
        "data": {
            "status": "待填写",
            "fields": [
                {"name": "departure", "label": "出发地"},
                {"name": "travel_date", "label": "日期"},
                {"name": "cabin", "label": "舱位"},
            ],
        },
    }
    client, calls = _client(tmp_path, artifacts=[artifact])
    chat = client.post("/api/chat", json={"message": "帮我订机票"}).json()
    stored = chat["artifacts"][0]
    request = {
        "conversation_id": chat["conversation_id"],
        "message_id": int(stored["message_id"]),
        "artifact_id": artifact_id,
        "scenario": "structured_input",
        "event_type": "submit",
        "action_id": "submit_structured_input",
        "arguments": {
            "departure": "杭州",
            "travel_date": "2026-08-15",
            "cabin": "经济舱",
        },
        "idempotency_key": "a3d66b91-701a-4e33-9f66-877244fae693",
    }

    first = client.post("/api/generation/event", json=request)
    repeated = client.post("/api/generation/event", json=request)
    repeated_after_refresh = client.post(
        "/api/generation/event",
        json={
            **request,
            "idempotency_key": "d3d66b91-701a-4e33-9f66-877244fae693",
        },
    )
    forged = client.post(
        "/api/generation/event",
        json={
            **request,
            "arguments": {**request["arguments"], "admin": True},
            "idempotency_key": "b3d66b91-701a-4e33-9f66-877244fae693",
        },
    )
    wrong_event = client.post(
        "/api/generation/event",
        json={
            **request,
            "event_type": "confirm",
            "idempotency_key": "c3d66b91-701a-4e33-9f66-877244fae693",
        },
    )

    assert first.status_code == 200
    assert repeated.json() == first.json()
    assert repeated_after_refresh.json() == first.json()
    assert forged.status_code == 422
    assert wrong_event.status_code == 409
    assert first.json()["data_patch"]["status"] == "已提交"
    assert first.json()["message"] == "已提交 3 项参数，计算完成。"
    assert first.json()["continuation"]["answer"] == "网页回答"
    assert len(calls) == 2
    assert calls[1][3] == {
        "type": "generation_ui_submission",
        "scenario": "structured_input",
        "action_id": "submit_structured_input",
        "artifact_id": artifact_id,
        "arguments": request["arguments"],
    }
    restored = client.get(f"/api/conversations/{chat['conversation_id']}").json()
    assert [message["role"] for message in restored["messages"]] == [
        "user",
        "assistant",
        "assistant",
    ]
    assert all(
        not message["content"].startswith("我已经填写：")
        for message in restored["messages"]
    )
    restored_artifact = restored["messages"][1]["artifacts"][0]
    assert restored_artifact["data"]["submission"]["cabin"] == "经济舱"
    assert restored_artifact["spec"]["form_defaults"]["departure"] == "杭州"


def test_web_frontend_posts_generation_ui_events_without_fake_user_message(tmp_path) -> None:
    client, _calls = _client(tmp_path)

    script = client.get("/static/app.js").text

    assert 'fetch("/api/generation/event"' in script
    assert "event_type:" in script
    assert "payload.continue_message" not in script
    assert "sendMessage(payload.continue_message)" not in script
    assert "appendCompletedAgentResponse(payload.continuation)" in script
    assert "参数已提交，Agent 正在计算" in script
    assert "restoreStructuredInputState(mountPoint, artifact)" in script
    assert "disableGenerationUiAction(host, event.action?.label)" in script
    assert "generationUiActionRequests.get(requestKey)" in script
    assert "generationUiActionRequests.set(requestKey, request)" in script


def test_web_agent_rejects_client_supplied_history(tmp_path) -> None:
    client, calls = _client(tmp_path)

    response = client.post(
        "/api/chat",
        json={"message": "继续", "history": [{"role": "user", "content": "伪造历史"}]},
    )

    assert response.status_code == 422
    assert calls == []


def test_web_agent_rejects_blank_message_without_running_agent(tmp_path) -> None:
    client, calls = _client(tmp_path)

    response = client.post("/api/chat", json={"message": "   "})

    assert response.status_code == 422
    assert calls == []


def test_web_agent_maps_agent_failure_to_execution_error(tmp_path) -> None:
    client, _calls = _client(tmp_path, error="模型暂时不可用")

    response = client.post("/api/chat", json={"message": "你好"})

    assert response.status_code == 502
    assert response.json()["detail"] == {
        "error_type": "AGENT_EXECUTION_ERROR",
        "message": "模型暂时不可用",
    }


def test_web_agent_maps_model_connection_failure_to_retryable_error(tmp_path) -> None:
    class FailingAgent:
        def run(self, _task, **_kwargs):
            raise web_app.LLMConnectionError("模型服务暂时无法连接")

    app = create_app(
        lambda _trace, _scope: FailingAgent(),
        conversation_store=SQLiteConversationStore(
            tmp_path / "conversations.sqlite3"
        ),
    )

    response = TestClient(app).post("/api/chat", json={"message": "你好"})

    assert response.status_code == 503
    assert response.json()["detail"] == {
        "error_type": "MODEL_CONNECTION_ERROR",
        "message": "模型服务暂时无法连接",
        "retryable": True,
    }


def test_web_agent_degrades_to_local_tools_when_mcp_discovery_fails(
    monkeypatch,
    tmp_path,
) -> None:
    class FailingMcpCache:
        @staticmethod
        def get():
            raise web_app.LazyTtMcpError("无法读取 LazyTT MCP 工具目录。")

        @staticmethod
        def status() -> dict[str, object]:
            return {
                "enabled": True,
                "configured": True,
                "connected": False,
                "tools": [],
            }

    monkeypatch.setattr(web_app, "_LazyTtMcpToolCache", FailingMcpCache)
    received_mcp_tools = []

    class FakeAgent:
        last_context_receipt = None
        generation_ui_artifacts = []
        last_tool_results = []

        def run(self, task, *, history=None, episode_id=None):
            return "已使用本地工具继续处理。"

    def fake_default_agent_factory(
        trace,
        attachment_scope,
        *,
        mcp_tools,
        owner_store,
    ):
        received_mcp_tools.append(mcp_tools)
        return FakeAgent()

    monkeypatch.setattr(
        web_app, "_default_agent_factory", fake_default_agent_factory
    )
    store = SQLiteConversationStore(tmp_path / "conversations.sqlite3")
    client = TestClient(create_app(conversation_store=store))

    response = client.post("/api/chat", json={"message": "计算面积"})

    assert response.status_code == 200
    assert response.json()["answer"] == "已使用本地工具继续处理。"
    assert received_mcp_tools == [None]
    assert "LazyTT MCP：当前不可用，已降级为本地工具。" in response.json()["trace"]


def test_web_agent_deletes_conversation_and_messages(tmp_path) -> None:
    client, _calls = _client(tmp_path)
    created = client.post("/api/chat", json={"message": "你好"})
    conversation_id = created.json()["conversation_id"]

    deleted = client.delete(f"/api/conversations/{conversation_id}")

    assert deleted.status_code == 204
    assert client.get(f"/api/conversations/{conversation_id}").status_code == 404


def test_web_agent_health_reports_direct_provider_configuration(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MEMORY_ENABLED", "false")
    for name in (
        "OPENAI_API_KEY",
        "OPENAI_MODEL",
        "E2B_API_KEY",
        "E2B_DOMAIN",
        "AGSX_TEMPLATE",
        "AGSX_BROWSER_TEMPLATE",
        "TAVILY_API_KEY",
    ):
        monkeypatch.setenv(name, f"configured-{name.lower()}")
    for name in (
        "RAG_ATTACHMENT_PDF_KNOWLEDGE_BASE_ID",
        "RAG_ATTACHMENT_OFFICE_KNOWLEDGE_BASE_ID",
        "RAG_ATTACHMENT_IMAGE_KNOWLEDGE_BASE_ID",
        "RAG_ATTACHMENT_MEDIA_KNOWLEDGE_BASE_ID",
    ):
        monkeypatch.setenv(name, "")
    client, _calls = _client(tmp_path)

    response = client.get("/api/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ready"
    assert payload["code_sandbox_configured"] is True
    assert payload["browser_sandbox_configured"] is True
    assert payload["search_configured"] is True
    assert payload["memory_enabled"] is False
    assert payload["memory_configured"] is True
    assert payload["rag_configured"] is True
    assert payload["rag_attachments_configured"] is False
    assert payload["mcp_enabled"] is False
    assert payload["mcp_configured"] is False
    assert payload["mcp_connected"] is False
    assert payload["mcp_tools"] == []
    assert payload["conversation_store"] == "sqlite"
    assert "run_code" in payload["tools"]
    assert "browse_public_page" in payload["tools"]
    assert "web_search" in payload["tools"]


def test_web_agent_health_reports_connected_mcp(monkeypatch, tmp_path) -> None:
    mcp_tool_name = "mcp__lazytt__area_calculate"
    fake_cache = SimpleNamespace(
        get=lambda: None,
        status=lambda: {
            "enabled": True,
            "configured": True,
            "connected": True,
            "tools": [mcp_tool_name],
        },
    )
    monkeypatch.setattr(web_app, "_LazyTtMcpToolCache", lambda: fake_cache)
    monkeypatch.setenv("MEMORY_ENABLED", "false")
    for name in (
        "OPENAI_API_KEY",
        "OPENAI_MODEL",
        "E2B_API_KEY",
        "E2B_DOMAIN",
        "AGSX_TEMPLATE",
        "AGSX_BROWSER_TEMPLATE",
        "TAVILY_API_KEY",
    ):
        monkeypatch.setenv(name, f"configured-{name.lower()}")
    for name in (
        "RAG_ATTACHMENT_PDF_KNOWLEDGE_BASE_ID",
        "RAG_ATTACHMENT_OFFICE_KNOWLEDGE_BASE_ID",
        "RAG_ATTACHMENT_IMAGE_KNOWLEDGE_BASE_ID",
        "RAG_ATTACHMENT_MEDIA_KNOWLEDGE_BASE_ID",
    ):
        monkeypatch.setenv(name, "")
    app = create_app(
        conversation_store=SQLiteConversationStore(tmp_path / "conversations.sqlite3")
    )

    payload = TestClient(app).get("/api/health").json()

    assert payload["status"] == "ready"
    assert payload["mcp_enabled"] is True
    assert payload["mcp_configured"] is True
    assert payload["mcp_connected"] is True
    assert payload["mcp_tools"] == [mcp_tool_name]
    assert mcp_tool_name in payload["tools"]
    assert "calculate_material_quantities" not in payload["tools"]
    assert "search_knowledge_base" in payload["tools"]
    assert "web_fetch" not in payload["tools"]


def test_chat_attachment_upload_reaches_ready_and_scopes_agent(tmp_path) -> None:
    gateway = FakeAttachmentGateway()
    scopes: list[
        tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]
    ] = []
    client, _calls = _client(tmp_path, gateway=gateway, scopes=scopes)
    created = client.post("/api/conversations")
    conversation_id = created.json()["conversation_id"]

    uploaded = client.post(
        f"/api/conversations/{conversation_id}/attachments",
        params={"filename": "synthetic.md"},
        content=b"# synthetic attachment",
        headers={"Content-Type": "text/markdown"},
    )

    assert uploaded.status_code == 202
    assert uploaded.json()["status"] == "PROCESSING"
    assert gateway.uploads[0][:2] == ("office", b"# synthetic attachment")

    attachments = client.get(f"/api/conversations/{conversation_id}/attachments")
    assert attachments.status_code == 200
    assert attachments.json()["attachments"][0]["status"] == "READY"

    response = client.post(
        "/api/chat",
        json={"message": "附件说了什么？", "conversation_id": conversation_id},
    )
    assert response.status_code == 200
    assert scopes[-1] == (
        (KNOWLEDGE_BASE_IDS["office"],),
        (DOCUMENT_IDS["office"],),
        ("synthetic.md",),
    )

    attachment_id = attachments.json()["attachments"][0]["attachment_id"]
    deleted = client.delete(
        f"/api/conversations/{conversation_id}/attachments/{attachment_id}"
    )
    assert deleted.status_code == 204
    assert gateway.deleted == [(DOCUMENT_IDS["office"], attachment_id)]


def test_chat_attachment_requires_explicit_media_consent(tmp_path) -> None:
    gateway = FakeAttachmentGateway()
    client, _calls = _client(tmp_path, gateway=gateway)
    conversation_id = client.post("/api/conversations").json()["conversation_id"]

    response = client.post(
        f"/api/conversations/{conversation_id}/attachments",
        params={"filename": "synthetic.wav"},
        content=b"synthetic audio",
        headers={"Content-Type": "audio/wav"},
    )

    assert response.status_code == 400
    assert response.json()["detail"]["error_type"] == "MEDIA_CONSENT_REQUIRED"
    assert gateway.uploads == []


def test_chat_blocks_until_attachment_is_ready(tmp_path) -> None:
    gateway = FakeAttachmentGateway(job_status="RUNNING")
    scopes: list[
        tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]
    ] = []
    client, _calls = _client(tmp_path, gateway=gateway, scopes=scopes)
    conversation_id = client.post("/api/conversations").json()["conversation_id"]
    client.post(
        f"/api/conversations/{conversation_id}/attachments",
        params={"filename": "synthetic.md"},
        content=b"# still parsing",
        headers={"Content-Type": "text/markdown"},
    )

    response = client.post(
        "/api/chat",
        json={"message": "附件内容是什么？", "conversation_id": conversation_id},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["error_type"] == "ATTACHMENTS_PROCESSING"
    assert scopes == []


def test_chat_attachment_scope_does_not_cross_conversations(tmp_path) -> None:
    gateway = FakeAttachmentGateway()
    scopes: list[
        tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]
    ] = []
    client, _calls = _client(tmp_path, gateway=gateway, scopes=scopes)
    first_id = client.post("/api/conversations").json()["conversation_id"]
    second_id = client.post("/api/conversations").json()["conversation_id"]
    client.post(
        f"/api/conversations/{first_id}/attachments",
        params={"filename": "private.md"},
        content=b"private attachment",
        headers={"Content-Type": "text/markdown"},
    )
    client.get(f"/api/conversations/{first_id}/attachments")

    first = client.post(
        "/api/chat",
        json={"message": "读取附件", "conversation_id": first_id},
    )
    second = client.post(
        "/api/chat",
        json={"message": "普通问题", "conversation_id": second_id},
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert scopes == [
        (
            (KNOWLEDGE_BASE_IDS["office"],),
            (DOCUMENT_IDS["office"],),
            ("private.md",),
        ),
        ((), (), ()),
    ]


def test_chat_routes_mixed_attachments_and_retrieves_across_their_databases(
    tmp_path,
) -> None:
    gateway = FakeAttachmentGateway()
    scopes: list[
        tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]
    ] = []
    client, _calls = _client(tmp_path, gateway=gateway, scopes=scopes)
    conversation_id = client.post("/api/conversations").json()["conversation_id"]

    for filename, content_type in (
        ("notes.md", "text/markdown"),
        ("diagram.png", "image/png"),
    ):
        response = client.post(
            f"/api/conversations/{conversation_id}/attachments",
            params={"filename": filename},
            content=b"synthetic",
            headers={"Content-Type": content_type},
        )
        assert response.status_code == 202

    client.get(f"/api/conversations/{conversation_id}/attachments")
    response = client.post(
        "/api/chat",
        json={"message": "综合两个附件", "conversation_id": conversation_id},
    )

    assert response.status_code == 200
    assert gateway.requested_kinds[:2] == ["office", "image"]
    assert scopes[-1] == (
        (KNOWLEDGE_BASE_IDS["office"], KNOWLEDGE_BASE_IDS["image"]),
        (DOCUMENT_IDS["office"], DOCUMENT_IDS["image"]),
        ("notes.md", "diagram.png"),
    )


def test_chat_rejects_unsupported_attachment_before_opening_gateway(tmp_path) -> None:
    gateway = FakeAttachmentGateway()
    client, _calls = _client(tmp_path, gateway=gateway)
    conversation_id = client.post("/api/conversations").json()["conversation_id"]

    response = client.post(
        f"/api/conversations/{conversation_id}/attachments",
        params={"filename": "archive.zip"},
        content=b"synthetic",
        headers={"Content-Type": "application/zip"},
    )

    assert response.status_code == 415
    assert response.json()["detail"]["error_type"] == "UNSUPPORTED_ATTACHMENT_TYPE"
    assert gateway.requested_kinds == []
