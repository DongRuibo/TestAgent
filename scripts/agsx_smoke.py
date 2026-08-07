"""腾讯云 AGSX Code Interpreter 的最小端到端验证。

这个脚本故意独立于 Agent Loop：先验证云沙箱的创建、代码执行、隔离和
销毁，再把它封装成模型可调用的工具，便于区分云接入问题和 Agent 协议问题。
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv


def require_env(name: str) -> str:
    """读取必填环境变量，但不输出可能包含密钥的值。"""
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def require_e2b_api_key() -> str:
    """拒绝会被当前 E2B SDK 在发请求前拦截的旧格式。"""
    api_key = require_env("E2B_API_KEY")
    if not api_key.startswith("e2b_"):
        raise SystemExit(
            "E2B_API_KEY must use the E2B-compatible value beginning with e2b_. "
            "Create or open an AGSX API Key and copy its E2B SDK format; "
            "do not paste the key into chat."
        )
    return api_key


def main() -> None:
    """创建一次性 AGSX 实例，执行探针代码，并确保实例最终被销毁。"""
    # 先加载 .env，再导入 SDK，确保 SDK 能读到 E2B_API_KEY 和 E2B_DOMAIN。
    load_dotenv()
    require_e2b_api_key()
    require_env("E2B_DOMAIN")
    template = require_env("AGSX_TEMPLATE")

    from e2b_code_interpreter import Sandbox

    # 远端和本地使用同名随机文件。远端写入后，本地不应出现该文件。
    marker_name = f"testagent-agsx-{uuid4().hex}.txt"
    marker_value = uuid4().hex
    remote_marker = f"/tmp/{marker_name}"
    local_marker = Path("/tmp") / marker_name

    python_code = f'''\
from pathlib import Path
import platform
import sys

marker = Path({remote_marker!r})
marker.write_text({marker_value!r}, encoding="utf-8")

print("AGSX_SMOKE_OK")
print(f"python={{sys.version.split()[0]}}")
print(f"platform={{platform.system()}}")
print(f"cwd={{Path.cwd()}}")
print(f"marker={{marker.read_text(encoding='utf-8')}}")
'''

    sandbox: Sandbox | None = None
    try:
        print(f"Creating AGSX sandbox from template: {template}")
        sandbox = Sandbox.create(template=template, timeout=300)
        print(f"Sandbox created: {sandbox.sandbox_id}")

        execution = sandbox.run_code(python_code, timeout=30)
        stdout = "".join(execution.logs.stdout)
        stderr = "".join(execution.logs.stderr)

        if stdout:
            print("--- remote stdout ---")
            print(stdout, end="" if stdout.endswith("\n") else "\n")
        if stderr:
            print("--- remote stderr ---")
            print(stderr, end="" if stderr.endswith("\n") else "\n")
        if execution.error is not None:
            raise RuntimeError(f"Remote execution failed: {execution.error}")
        if "AGSX_SMOKE_OK" not in stdout or marker_value not in stdout:
            raise RuntimeError("Remote output did not contain the expected probe values.")
        if local_marker.exists():
            raise RuntimeError("Isolation check failed: remote marker appeared locally.")

        print("Isolation check passed: the remote /tmp file does not exist locally.")
    finally:
        if sandbox is not None:
            killed = sandbox.kill()
            print(f"Sandbox destroyed: {killed}")


if __name__ == "__main__":
    main()
