"""管理可跨 Robot 任务复用的小红书专用 Chrome。"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .exceptions import ConfigurationError, JobUnavailableError
from .environment import subprocess_environment


@dataclass(frozen=True)
class ManagedBrowserSession:
    """记录模块启动并可安全复用的本地 Chrome 会话。"""

    pid: int
    port: int
    profile_dir: Path
    target_id: str = ""


class XiaohongshuBrowserSessionManager:
    """启动、探测并复用唯一的小红书专用 Chrome。"""

    def __init__(
        self,
        data_dir: Path,
        *,
        chrome_path: Path | None = None,
        startup_timeout_seconds: float = 15.0,
    ) -> None:
        self.data_dir = data_dir.resolve()
        self.profile_dir = self.data_dir / "browser_profiles" / "xiaohongshu"
        self.session_file = self.data_dir / "browser_sessions" / "xiaohongshu.json"
        self.chrome_path = chrome_path or self._find_chrome()
        self.startup_timeout_seconds = startup_timeout_seconds

    def ensure(self) -> ManagedBrowserSession:
        """复用健康会话；关闭后自动重启；拒绝接管外部占用。"""

        session = self._read_session()
        if session is not None and self._session_is_healthy(session):
            return self._ensure_managed_page(session)
        if self._profile_is_locked() and not self._lock_is_from_exited_managed_browser(session):
            raise JobUnavailableError(
                "小红书专用 Profile 已被非托管 Chrome 占用；"
                "请关闭该窗口后重试，不要删除 SingletonLock"
            )
        self._remove_stale_session_file()
        return self._launch()

    def state(self) -> str:
        """返回不含端口、PID 或 Profile 内容的浏览器状态。"""

        session = self._read_session()
        if session is not None and self._session_is_healthy(session):
            return "managed_ready"
        if self._profile_is_locked() and not self._lock_is_from_exited_managed_browser(session):
            return "occupied_external"
        return "closed"

    def _launch(self) -> ManagedBrowserSession:
        """用本地回环调试端口启动新的受控 Chrome。"""

        if not self.chrome_path.is_file():
            raise ConfigurationError("未检测到 Google Chrome，无法启动专用浏览器")
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        port = self._allocate_loopback_port()
        command = [
            str(self.chrome_path),
            f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={self.profile_dir}",
            "--profile-directory=Default",
            "--no-first-run",
            "--no-default-browser-check",
            "about:blank",
        ]
        try:
            process = subprocess.Popen(
                command,
                env=subprocess_environment(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            raise JobUnavailableError("小红书专用 Chrome 启动失败") from exc

        deadline = time.monotonic() + self.startup_timeout_seconds
        while True:
            if process.poll() is not None:
                raise JobUnavailableError("小红书专用 Chrome 启动后意外退出")
            if self._probe_debug_endpoint(port):
                session = ManagedBrowserSession(process.pid, port, self.profile_dir)
                return self._ensure_managed_page(session)
            if time.monotonic() >= deadline:
                self._terminate_started_process(process)
                raise JobUnavailableError("等待小红书专用 Chrome 启动超时")
            time.sleep(0.2)

    def _session_is_healthy(self, session: ManagedBrowserSession) -> bool:
        return (
            session.profile_dir == self.profile_dir
            and self._pid_is_alive(session.pid)
            and self._probe_debug_endpoint(session.port)
        )

    def _read_session(self) -> ManagedBrowserSession | None:
        if not self.session_file.is_file():
            return None
        try:
            payload = json.loads(self.session_file.read_text(encoding="utf-8"))
            pid = int(payload["pid"])
            port = int(payload["port"])
            profile_dir = Path(str(payload["profile_dir"])).resolve()
            target_id = str(payload.get("target_id", ""))
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
            return None
        if pid <= 0 or not 1024 <= port <= 65535:
            return None
        return ManagedBrowserSession(pid, port, profile_dir, target_id)

    def _write_session(self, session: ManagedBrowserSession) -> None:
        self.session_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.session_file.with_suffix(".tmp")
        payload: dict[str, Any] = {
            "pid": session.pid,
            "port": session.port,
            "profile_dir": str(session.profile_dir),
            "target_id": session.target_id,
        }
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temporary.replace(self.session_file)

    def _remove_stale_session_file(self) -> None:
        try:
            self.session_file.unlink()
        except FileNotFoundError:
            return

    def _profile_is_locked(self) -> bool:
        lock = self.profile_dir / "SingletonLock"
        return lock.exists() or lock.is_symlink()

    def _lock_is_from_exited_managed_browser(self, session: ManagedBrowserSession | None) -> bool:
        """仅放行本机已退出托管进程的残留锁，由 Chrome 自己回收，绝不删除锁。"""

        if session is None or session.profile_dir != self.profile_dir or self._pid_is_alive(session.pid):
            return False
        try:
            host, pid = str((self.profile_dir / "SingletonLock").readlink()).rsplit("-", 1)
        except (OSError, ValueError):
            return False
        return host == socket.gethostname() and pid == str(session.pid)

    def _probe_debug_endpoint(self, port: int) -> bool:
        payload = self._request_json(port, "/json/version")
        return isinstance(payload, dict) and str(payload.get("webSocketDebuggerUrl", "")).startswith(
            "ws://"
        )

    def _ensure_managed_page(self, session: ManagedBrowserSession) -> ManagedBrowserSession:
        targets = self._request_json(session.port, "/json/list")
        if isinstance(targets, list) and session.target_id:
            if any(
                isinstance(target, dict)
                and target.get("type") == "page"
                and target.get("id") == session.target_id
                for target in targets
            ):
                return session
        created = self._request_json(session.port, "/json/new?about:blank", method="PUT")
        if not isinstance(created, dict) or not str(created.get("id", "")).strip():
            raise JobUnavailableError("无法创建小红书专用自动化标签页")
        updated = ManagedBrowserSession(
            session.pid,
            session.port,
            session.profile_dir,
            str(created["id"]),
        )
        self._write_session(updated)
        return updated

    def _request_json(self, port: int, path: str, *, method: str = "GET") -> Any | None:
        try:
            request = Request(f"http://127.0.0.1:{port}{path}", method=method)
            with urlopen(request, timeout=0.5) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        return payload

    def _allocate_loopback_port(self) -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.bind(("127.0.0.1", 0))
            return int(server.getsockname()[1])

    def _pid_is_alive(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _terminate_started_process(self, process: subprocess.Popen[bytes]) -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return

    def _find_chrome(self) -> Path:
        macos_chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        if macos_chrome.is_file():
            return macos_chrome
        executable = shutil.which("google-chrome")
        return Path(executable) if executable else macos_chrome
