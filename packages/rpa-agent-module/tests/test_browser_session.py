from __future__ import annotations

import json
from pathlib import Path

import pytest

from rpa_agent_module.browser_session import (
    ManagedBrowserSession,
    XiaohongshuBrowserSessionManager,
)
from rpa_agent_module.exceptions import JobUnavailableError


def make_manager(tmp_path: Path) -> XiaohongshuBrowserSessionManager:
    chrome = tmp_path / "Google Chrome"
    chrome.write_text("fake", encoding="utf-8")
    return XiaohongshuBrowserSessionManager(tmp_path, chrome_path=chrome)


def test_reuses_healthy_managed_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = make_manager(tmp_path)
    expected = ManagedBrowserSession(1234, 9222, manager.profile_dir, "page-1")
    manager._write_session(expected)
    monkeypatch.setattr(manager, "_pid_is_alive", lambda _pid: True)
    monkeypatch.setattr(manager, "_probe_debug_endpoint", lambda _port: True)
    monkeypatch.setattr(manager, "_ensure_managed_page", lambda session: session)
    monkeypatch.setattr(
        manager,
        "_launch",
        lambda: pytest.fail("健康会话不应重新启动 Chrome"),
    )

    assert manager.ensure() == expected
    assert manager.state() == "managed_ready"


def test_closed_managed_browser_is_restarted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = make_manager(tmp_path)
    stale = ManagedBrowserSession(1234, 9222, manager.profile_dir, "old-page")
    replacement = ManagedBrowserSession(5678, 9333, manager.profile_dir, "new-page")
    manager._write_session(stale)
    monkeypatch.setattr(manager, "_pid_is_alive", lambda _pid: False)
    monkeypatch.setattr(manager, "_launch", lambda: replacement)

    assert manager.ensure() == replacement
    assert not manager.session_file.exists()


def test_external_profile_lock_is_never_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = make_manager(tmp_path)
    manager.profile_dir.mkdir(parents=True)
    profile_lock = manager.profile_dir / "SingletonLock"
    profile_lock.write_text("external", encoding="utf-8")
    monkeypatch.setattr(
        manager,
        "_launch",
        lambda: pytest.fail("外部占用时不应启动 Chrome"),
    )

    with pytest.raises(JobUnavailableError, match="非托管 Chrome"):
        manager.ensure()

    assert profile_lock.is_file()
    assert manager.state() == "occupied_external"


def test_first_use_launches_loopback_managed_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = make_manager(tmp_path)
    captured: dict[str, object] = {}

    class FakeProcess:
        pid = 2468

        def poll(self) -> None:
            return None

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr("rpa_agent_module.browser_session.subprocess.Popen", fake_popen)
    monkeypatch.setattr(manager, "_allocate_loopback_port", lambda: 9444)
    monkeypatch.setattr(manager, "_probe_debug_endpoint", lambda _port: True)

    def ensure_page(session: ManagedBrowserSession) -> ManagedBrowserSession:
        updated = ManagedBrowserSession(
            session.pid, session.port, session.profile_dir, "page-1"
        )
        manager._write_session(updated)
        return updated

    monkeypatch.setattr(
        manager,
        "_ensure_managed_page",
        ensure_page,
    )

    session = manager.ensure()

    assert session == ManagedBrowserSession(2468, 9444, manager.profile_dir, "page-1")
    command = captured["command"]
    assert "--remote-debugging-port=9444" in command
    assert "--remote-debugging-address=127.0.0.1" in command
    assert f"--user-data-dir={manager.profile_dir}" in command
    saved = json.loads(manager.session_file.read_text(encoding="utf-8"))
    assert saved == {
        "pid": 2468,
        "port": 9444,
        "profile_dir": str(manager.profile_dir),
        "target_id": "page-1",
    }


def test_closed_managed_tab_is_recreated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = make_manager(tmp_path)
    session = ManagedBrowserSession(1234, 9222, manager.profile_dir, "closed-page")
    responses = {
        ("/json/list", "GET"): [{"id": "other-page", "type": "page"}],
        ("/json/new?about:blank", "PUT"): {"id": "replacement-page", "type": "page"},
    }
    monkeypatch.setattr(
        manager,
        "_request_json",
        lambda _port, path, method="GET": responses[(path, method)],
    )

    updated = manager._ensure_managed_page(session)

    assert updated.target_id == "replacement-page"
    assert json.loads(manager.session_file.read_text(encoding="utf-8"))["target_id"] == (
        "replacement-page"
    )
