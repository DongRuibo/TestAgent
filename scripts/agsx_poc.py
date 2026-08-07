"""腾讯云 AGSX Code Interpreter 的单实例综合 POC。

在同一个沙箱中验证数据分析、Shell、文件系统、本地密钥隔离和 SANDBOX
网络出站策略，同时记录创建、执行、销毁与总耗时。
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from dotenv import load_dotenv


SALES_CODE = """
import json
import platform

sales = [120, 135, 128, 160, 175, 210]
result = {
    "python_version": platform.python_version(),
    "first_month": sales[0],
    "last_month": sales[-1],
    "total": sum(sales),
    "average": round(sum(sales) / len(sales), 2),
    "growth_percent": round((sales[-1] / sales[0] - 1) * 100, 2),
}
print(json.dumps(result, ensure_ascii=False))
"""

ENV_PROBE_CODE = """
import json
import os

names = ["OPENAI_API_KEY", "E2B_API_KEY", "AGSX_TEMPLATE"]
print(json.dumps({name: name in os.environ for name in names}))
"""

NETWORK_PROBE_CODE = """
import json
import urllib.error
import urllib.request

try:
    response = urllib.request.urlopen("https://www.qq.com", timeout=5)
    result = {"public_internet_accessible": True, "status": response.status}
except urllib.error.HTTPError as exc:
    result = {"public_internet_accessible": True, "status": exc.code}
except Exception as exc:
    result = {
        "public_internet_accessible": False,
        "error_type": type(exc).__name__,
        "message": str(exc),
    }

print(json.dumps(result, ensure_ascii=False))
"""


def main() -> None:
    """运行一次综合验证，并始终尝试销毁创建成功的沙箱。"""
    load_dotenv()
    api_key = _require_env("E2B_API_KEY")
    domain = _require_env("E2B_DOMAIN")
    template = _require_env("AGSX_TEMPLATE")
    if not api_key.startswith("e2b_"):
        raise SystemExit("E2B_API_KEY must use the e2b_ compatible format.")

    from e2b_code_interpreter import Sandbox

    report: dict[str, Any] = {
        "template": template,
        "expected_network_mode": "SANDBOX",
        "status": "running",
    }
    sandbox: Sandbox | None = None
    started_at = time.perf_counter()

    try:
        create_started = time.perf_counter()
        sandbox = Sandbox.create(
            template=template,
            timeout=300,
            api_key=api_key,
            domain=domain,
        )
        report["sandbox_id"] = sandbox.sandbox_id
        report["create_seconds"] = _elapsed(create_started)

        sales_started = time.perf_counter()
        sales = _run_json(sandbox, SALES_CODE)
        report["sales_analysis"] = sales
        report["sales_seconds"] = _elapsed(sales_started)
        expected_sales = {
            "first_month": 120,
            "last_month": 210,
            "total": 928,
            "average": 154.67,
            "growth_percent": 75.0,
        }
        for key, expected in expected_sales.items():
            if sales.get(key) != expected:
                raise RuntimeError(
                    f"Sales assertion failed for {key}: {sales.get(key)!r} != {expected!r}"
                )

        shell_started = time.perf_counter()
        command = sandbox.commands.run(
            "python --version && pwd && "
            "echo 'hello AGSX' > /tmp/agsx-poc.txt && "
            "cat /tmp/agsx-poc.txt",
            timeout=20,
        )
        report["shell"] = {
            "exit_code": command.exit_code,
            "stdout": command.stdout,
            "stderr": command.stderr,
        }
        report["shell_seconds"] = _elapsed(shell_started)
        if command.exit_code != 0 or "hello AGSX" not in command.stdout:
            raise RuntimeError("Shell or in-sandbox file probe failed.")

        env_probe = _run_json(sandbox, ENV_PROBE_CODE)
        report["local_secrets_present_in_sandbox"] = env_probe
        if any(env_probe.values()):
            raise RuntimeError("A local secret environment variable reached the sandbox.")

        network_started = time.perf_counter()
        network_probe = _run_json(sandbox, NETWORK_PROBE_CODE)
        report["network_probe"] = network_probe
        report["network_seconds"] = _elapsed(network_started)
        if network_probe.get("public_internet_accessible"):
            raise RuntimeError(
                "Public Internet was reachable although this POC expects SANDBOX mode."
            )

        report["status"] = "passed"
    except Exception as exc:
        report["status"] = "failed"
        report["failure"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        if sandbox is not None:
            kill_started = time.perf_counter()
            try:
                report["sandbox_destroyed"] = bool(sandbox.kill())
            except Exception as exc:
                report["sandbox_destroyed"] = False
                report["cleanup_error"] = f"{type(exc).__name__}: {exc}"
            report["kill_seconds"] = _elapsed(kill_started)
        else:
            report["sandbox_destroyed"] = False
        report["total_seconds"] = _elapsed(started_at)
        print(json.dumps(report, ensure_ascii=False, indent=2))


def _run_json(sandbox: Any, code: str) -> dict[str, Any]:
    """执行 Python，检查远端错误，并解析最后一个非空 stdout 行。"""
    execution = sandbox.run_code(code, timeout=30)
    if execution.error is not None:
        raise RuntimeError(f"Remote execution failed: {execution.error}")
    lines = [line.strip() for line in execution.logs.stdout if line.strip()]
    if not lines:
        raise RuntimeError("Remote execution returned no stdout.")
    result = json.loads(lines[-1])
    if not isinstance(result, dict):
        raise RuntimeError("Remote JSON result is not an object.")
    return result


def _elapsed(started_at: float) -> float:
    """以便于对比的秒数记录阶段耗时。"""
    return round(time.perf_counter() - started_at, 3)


def _require_env(name: str) -> str:
    """读取必填环境变量，但不输出密钥内容。"""
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


if __name__ == "__main__":
    main()
