"""AGSX run_code 工具的离线生命周期测试。"""

from __future__ import annotations

from types import SimpleNamespace

from tools.run_code import AGSXConfig, run_code


TEST_CONFIG = AGSXConfig(
    api_key="e2b_test-key",
    domain="sandbox.example.com",
    template="test-template",
)


class FakeSandbox:
    """记录代码执行和销毁行为，不访问真实云端。"""

    def __init__(
        self,
        execution=None,
        run_error: Exception | None = None,
        kill_result: bool = True,
        kill_error: Exception | None = None,
    ) -> None:
        self.execution = execution or make_execution()
        self.run_error = run_error
        self.kill_result = kill_result
        self.kill_error = kill_error
        self.calls: list[tuple[str, int]] = []
        self.killed = False

    def run_code(self, code: str, timeout: int):
        self.calls.append((code, timeout))
        if self.run_error is not None:
            raise self.run_error
        return self.execution

    def kill(self) -> bool:
        self.killed = True
        if self.kill_error is not None:
            raise self.kill_error
        return self.kill_result


def make_execution(
    stdout: list[str] | None = None,
    stderr: list[str] | None = None,
    error=None,
):
    """构造 E2B Execution 对象所需的最小字段。"""
    logs = SimpleNamespace(stdout=stdout or [], stderr=stderr or [])
    return SimpleNamespace(logs=logs, error=error, execution_count=1)


def test_run_code_executes_and_destroys_sandbox() -> None:
    sandbox = FakeSandbox(make_execution(stdout=["2\n"]))
    factory_calls = []

    def factory(config: AGSXConfig, timeout: int):
        factory_calls.append((config, timeout))
        return sandbox

    result = run_code(
        "print(1 + 1)",
        config=TEST_CONFIG,
        sandbox_factory=factory,
        instance_timeout=120,
        execution_timeout=7,
    )

    assert result["success"] is True
    assert result["stdout"] == "2\n"
    assert result["sandbox_destroyed"] is True
    assert result["receipt_path"].startswith("artifacts/agent-code-")
    assert result["sandbox_id"] is not None
    assert factory_calls == [(TEST_CONFIG, 120)]
    assert sandbox.calls == [("print(1 + 1)", 7)]
    assert sandbox.killed is True


def test_run_code_returns_remote_error_and_still_destroys_sandbox() -> None:
    remote_error = SimpleNamespace(name="ZeroDivisionError", value="division by zero")
    sandbox = FakeSandbox(make_execution(stderr=["traceback"], error=remote_error))

    result = run_code(
        "1 / 0",
        config=TEST_CONFIG,
        sandbox_factory=lambda _config, _timeout: sandbox,
    )

    assert result["success"] is False
    assert result["error_type"] == "REMOTE_EXECUTION_ERROR"
    assert result["stderr"] == "traceback"
    assert result["sandbox_destroyed"] is True
    assert sandbox.killed is True


def test_run_code_converts_sdk_exception_and_destroys_sandbox() -> None:
    sandbox = FakeSandbox(run_error=TimeoutError("execution timed out"))

    result = run_code(
        "while True: pass",
        config=TEST_CONFIG,
        sandbox_factory=lambda _config, _timeout: sandbox,
    )

    assert result["success"] is False
    assert result["error_type"] == "SANDBOX_EXECUTION_ERROR"
    assert result["retryable"] is True
    assert result["sandbox_destroyed"] is True
    assert sandbox.killed is True


def test_run_code_reports_cleanup_failure() -> None:
    sandbox = FakeSandbox(
        make_execution(stdout=["done\n"]),
        kill_error=RuntimeError("kill failed"),
    )

    result = run_code(
        "print('done')",
        config=TEST_CONFIG,
        sandbox_factory=lambda _config, _timeout: sandbox,
    )

    assert result["success"] is False
    assert result["error_type"] == "SANDBOX_CLEANUP_ERROR"
    assert result["sandbox_destroyed"] is False
    assert "kill failed" in result["message"]


def test_run_code_rejects_empty_and_oversized_code_without_creating_sandbox() -> None:
    calls = []

    def factory(_config: AGSXConfig, _timeout: int):
        calls.append(True)
        return FakeSandbox()

    empty = run_code("  ", config=TEST_CONFIG, sandbox_factory=factory)
    oversized = run_code(
        "x" * 11,
        config=TEST_CONFIG,
        sandbox_factory=factory,
        max_code_chars=10,
    )

    assert empty["error_type"] == "INVALID_CODE"
    assert oversized["error_type"] == "CODE_TOO_LARGE"
    assert empty["receipt_path"].startswith("artifacts/agent-code-")
    assert oversized["receipt_path"].startswith("artifacts/agent-code-")
    assert calls == []
