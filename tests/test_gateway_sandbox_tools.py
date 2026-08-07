"""Agent 的 Gateway 工具入口不绕过服务层，并限制本地表格路径。"""

from __future__ import annotations

from types import SimpleNamespace

from sandbox_gateway.models import ExecutionStatus
from tools.gateway_sandbox import analyze_spreadsheet, run_code


class FakeClient:
    def __init__(self) -> None:
        self.calls = []

    def execute_code(self, code, **kwargs):
        self.calls.append(("code", code, kwargs))
        return _view({"success": True, "stdout": "2\n"})

    def analyze_spreadsheet_file(self, name, content, **kwargs):
        self.calls.append(("spreadsheet", name, kwargs))
        return _view({"success": True, "analysis": {"summary": {"row_count": 1}}})


def _view(result):
    return SimpleNamespace(
        status=ExecutionStatus.SUCCEEDED,
        result=result,
        receipt={"cleanup": {"destroyed": True}},
        execution_id="exec-test",
        provider="fake",
        sandbox_id="sandbox-test",
        artifacts=[],
        error_type=None,
        error_message=None,
    )


def test_run_code_uses_gateway_client() -> None:
    client = FakeClient()
    result = run_code("print(1 + 1)", client=client)

    assert result["success"] is True
    assert result["provider"] == "fake"
    assert result["sandbox_destroyed"] is True
    assert client.calls[0][0] == "code"


def test_spreadsheet_tool_rejects_path_escape_before_gateway() -> None:
    client = FakeClient()
    result = analyze_spreadsheet("../.env", client=client)

    assert result["success"] is False
    assert result["error_type"] == "INVALID_SPREADSHEET_PATH"
    assert client.calls == []
