"""网页 Agent API 和静态入口的离线测试。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from web_agent.app import create_app


class FakeWebAgent:
    def __init__(self, trace, calls, *, error: str | None = None) -> None:
        self.trace = trace
        self.calls = calls
        self.error = error

    def run(self, task: str, *, history=None) -> str:
        self.calls.append((task, history))
        self.trace("Step 1: 调用模型")
        if self.error:
            raise RuntimeError(self.error)
        self.trace("Step 1: 模型给出最终回答")
        return "网页回答"


def _client(*, error: str | None = None):
    calls = []

    def factory(trace):
        return FakeWebAgent(trace, calls, error=error)

    return TestClient(create_app(factory)), calls


def test_web_agent_serves_chat_page_and_static_assets() -> None:
    client, _calls = _client()

    page = client.get("/")
    styles = client.get("/static/app.css")

    assert page.status_code == 200
    assert "和你的 Agent 对话" in page.text
    assert styles.status_code == 200
    assert "--accent" in styles.text


def test_web_agent_keeps_shell_fixed_and_clears_input_before_request() -> None:
    client, _calls = _client()

    styles = client.get("/static/app.css").text
    script = client.get("/static/app.js").text

    assert "html, body { margin: 0; height: 100%; min-height: 0; overflow: hidden" in styles
    assert "grid-template-rows: auto minmax(0, 1fr) auto" in styles
    assert ".messages { min-height: 0; overflow-y: auto" in styles
    send_start = script.index("async function sendMessage")
    clear_input = script.index('input.value = "";', send_start)
    request_start = script.index("await fetch", send_start)
    assert clear_input < request_start


def test_web_agent_returns_answer_trace_and_forwards_history() -> None:
    client, calls = _client()

    response = client.post(
        "/api/chat",
        json={
            "message": "继续",
            "history": [
                {"role": "user", "content": "你好"},
                {"role": "assistant", "content": "你好，我是 Agent。"},
            ],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["answer"] == "网页回答"
    assert payload["trace"] == [
        "Step 1: 调用模型",
        "Step 1: 模型给出最终回答",
    ]
    assert isinstance(payload["duration_ms"], int)
    assert calls == [
        (
            "继续",
            [
                {"role": "user", "content": "你好"},
                {"role": "assistant", "content": "你好，我是 Agent。"},
            ],
        )
    ]


def test_web_agent_rejects_blank_message_without_running_agent() -> None:
    client, calls = _client()

    response = client.post("/api/chat", json={"message": "   "})

    assert response.status_code == 422
    assert calls == []


def test_web_agent_maps_agent_failure_to_gateway_error() -> None:
    client, _calls = _client(error="模型暂时不可用")

    response = client.post("/api/chat", json={"message": "你好"})

    assert response.status_code == 502
    assert response.json()["detail"] == {
        "error_type": "AGENT_EXECUTION_ERROR",
        "message": "模型暂时不可用",
    }


def test_web_agent_health_is_offline_and_lists_tools() -> None:
    client, _calls = _client()

    response = client.get("/api/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] in {"ready", "configuration_required"}
    assert "run_code" in payload["tools"]
    assert "browse_public_page" in payload["tools"]
