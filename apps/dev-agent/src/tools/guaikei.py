"""受控调用本地 GUAIKEI 小红书 CLI。"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


GUAIKEI_CLI_DIR_ENV = "GUAIKEI_CLI_DIR"
GUAIKEI_TOKEN_ENV = "GUAIKEI_API_TOKEN"
DEFAULT_TIMEOUT_SECONDS = 45
MAX_STDOUT_CHARS = 1_000_000
PROVIDER_CODE_PATTERN = re.compile(r"^[A-Z0-9_-]{1,64}$")
# 外部 CLI 默认会重试创建任务；宿主只允许创建一次，查询重试仍沿用 CLI。
CLI_BOOTSTRAP = """
const path = require('node:path');
const constants = require(path.resolve('src/config/constants.js'));
constants.CREATE_MAX_ATTEMPTS = 1;
require(path.resolve(process.argv[1]));
"""
# 只使用原参数查询，不调用任何创建任务方法。结果复用原来的字段规范化。
QUERY_BOOTSTRAP = """
const path = require('node:path');
const constants = require(path.resolve('src/config/constants.js'));
constants.QUERY_MAX_ATTEMPTS = 1;
const args = process.argv.slice(2);
const value = flag => args[args.indexOf(flag) + 1];
const token = process.env.GUAIKEI_API_TOKEN;
async function query() {
  let results;
  switch (path.basename(process.argv[1])) {
    case 'search-cli.js':
      results = await require(path.resolve('src/api/search.js')).getSearchTask(token,
        require(path.resolve('src/validate/keyword.js')).cleanKeyword(value('--keyword')), 0, 0, 0, 10); break;
    case 'detail-cli.js':
      results = await require(path.resolve('src/api/detail.js')).getDetailTask(token,
        require(path.resolve('src/validate/url.js')).normalizeUrl(value('--url'))); break;
    case 'comment-cli.js':
      results = await require(path.resolve('src/api/comment.js')).getCommentTask(token,
        require(path.resolve('src/validate/url.js')).normalizeUrl(value('--url')), Number(value('--limit'))); break;
    default: throw new Error('Unsupported query');
  }
  process.stdout.write(JSON.stringify({status: 'success', results}));
}
query().catch(() => { process.stdout.write(JSON.stringify({status: 'error'})); process.exitCode = 1; });
"""


class GuaikeiError(RuntimeError):
    def __init__(
        self,
        error_type: str,
        message: str,
        *,
        retryable: bool = False,
        provider_code: str | int | None = None,
    ) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.retryable = retryable
        self.provider_code = _provider_code(provider_code)


@dataclass(frozen=True)
class GuaikeiCli:
    """只开放四个固定只读脚本，不接受任意命令或脚本路径。"""

    cli_dir: Path | None
    api_token: str | None
    node_binary: str = "node"
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    query_only: bool = False

    @classmethod
    def from_env(cls) -> "GuaikeiCli":
        raw_dir = os.getenv(GUAIKEI_CLI_DIR_ENV, "").strip()
        token = os.getenv(GUAIKEI_TOKEN_ENV, "").strip()
        return cls(
            cli_dir=Path(raw_dir) if raw_dir else None,
            api_token=token or None,
        )

    def search(self, keyword: str) -> dict[str, Any]:
        return self._run("search-cli.js", "--keyword", keyword)

    def detail(self, url: str) -> dict[str, Any]:
        return self._run("detail-cli.js", "--url", url)

    def comments(self, url: str, limit: int) -> dict[str, Any]:
        return self._run("comment-cli.js", "--url", url, "--limit", str(limit))

    def creator_posts(self, url: str, limit: int) -> dict[str, Any]:
        return self._run("post-cli.js", "--url", url, "--limit", str(limit))

    def _run(self, script_name: str, *arguments: str) -> dict[str, Any]:
        root, token, node = self._validated_configuration(script_name)
        script = root / "src" / "xiaohongshu" / script_name
        command = [node, "-e", QUERY_BOOTSTRAP if self.query_only else CLI_BOOTSTRAP, str(script), *arguments]
        try:
            completed = subprocess.run(
                command,
                cwd=root,
                env=_subprocess_environment(token),
                shell=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            stderr = exc.stderr or b""
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", errors="replace")
            created = "任务创建成功" in stderr
            raise GuaikeiError(
                "PROVIDER_RESULT_PENDING" if created else "PROVIDER_TIMEOUT",
                ("取数任务已创建，但尚未收到结果。请先查询已有任务，不要重复创建。"
                    if created else "GUAIKEI CLI 调用超时，任务是否已创建未知。请先核对已有任务，不要自动重试。"),
                retryable=False,
            ) from exc
        except OSError as exc:
            raise GuaikeiError(
                "PROVIDER_COMMAND_FAILED",
                "GUAIKEI CLI 无法启动。",
            ) from exc

        stdout = completed.stdout.strip()
        if not stdout or len(stdout) > MAX_STDOUT_CHARS:
            if completed.returncode != 0:
                raise GuaikeiError(
                    "PROVIDER_COMMAND_FAILED",
                    f"GUAIKEI CLI 退出码为 {completed.returncode}。",
                    retryable=completed.returncode in {75, 124},
                )
            raise GuaikeiError(
                "INVALID_PROVIDER_RESPONSE",
                "GUAIKEI CLI 返回为空或超过大小限制。",
            )
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise GuaikeiError(
                "INVALID_PROVIDER_RESPONSE",
                "GUAIKEI CLI 未返回合法 JSON。",
            ) from exc
        if not isinstance(payload, dict):
            raise GuaikeiError(
                "INVALID_PROVIDER_RESPONSE",
                "GUAIKEI CLI 顶层结果必须是 JSON 对象。",
            )
        if completed.returncode != 0:
            provider_code = _provider_code(
                payload.get("error_code", payload.get("errcode"))
            )
            suffix = f"（provider_code={provider_code}）" if provider_code else ""
            raise GuaikeiError(
                "PROVIDER_COMMAND_FAILED",
                f"GUAIKEI CLI 调用失败{suffix}。",
                retryable=(
                    completed.returncode in {75, 124}
                    or _retryable_provider_code(provider_code)
                ),
                provider_code=provider_code,
            )
        return payload

    def _validated_configuration(self, script_name: str) -> tuple[Path, str, str]:
        if self.cli_dir is None or not self.api_token:
            raise GuaikeiError(
                "PROVIDER_NOT_CONFIGURED",
                f"请配置 {GUAIKEI_CLI_DIR_ENV} 和 {GUAIKEI_TOKEN_ENV}。",
            )
        root = self.cli_dir.expanduser()
        if not root.is_absolute():
            raise GuaikeiError(
                "PROVIDER_NOT_CONFIGURED",
                f"{GUAIKEI_CLI_DIR_ENV} 必须是绝对路径。",
            )
        root = root.resolve()
        script = root / "src" / "xiaohongshu" / script_name
        if (
            not root.is_dir()
            or script.is_symlink()
            or not script.is_file()
            or not script.resolve().is_relative_to(root)
        ):
            raise GuaikeiError(
                "PROVIDER_NOT_CONFIGURED",
                "GUAIKEI CLI 目录缺少所需的只读脚本。",
            )
        node = shutil.which(self.node_binary)
        if node is None:
            raise GuaikeiError(
                "PROVIDER_NOT_CONFIGURED",
                "未找到 Node.js 可执行文件。",
            )
        if not 1 <= self.timeout_seconds <= 120:
            raise GuaikeiError(
                "PROVIDER_NOT_CONFIGURED",
                "GUAIKEI CLI 超时配置必须在 1-120 秒之间。",
            )
        return root, self.api_token, node


def _subprocess_environment(token: str) -> Mapping[str, str]:
    """只向第三方脚本传递运行所需的最小环境变量。"""
    environment = {GUAIKEI_TOKEN_ENV: token}
    for name in ("PATH", "TMPDIR", "LANG", "LC_ALL"):
        value = os.getenv(name)
        if value:
            environment[name] = value
    return environment


def _provider_code(value: str | int | None) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    normalized = str(value).strip().upper()
    return normalized if PROVIDER_CODE_PATTERN.fullmatch(normalized) else None


def _retryable_provider_code(value: str | None) -> bool:
    if value in {
        "TIMEOUT",
        "NETWORK_ERROR",
        "ENOTFOUND",
        "ECONNRESET",
        "EAI_AGAIN",
        "ETIMEDOUT",
        "429",
        "ERRCODE_423",
        "ERRCODE_429",
    }:
        return True
    if value and value.startswith("ERRCODE_"):
        try:
            return 0 < int(value.removeprefix("ERRCODE_")) <= 16
        except ValueError:
            return False
    return False


__all__ = ["GuaikeiCli", "GuaikeiError"]
