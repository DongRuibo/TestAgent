"""LazyTT MCP 适配器的离线测试。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from mcp.server.mcpserver import MCPServer

from tools import lazytt_mcp
from tools.lazytt_mcp import (
    LazyTtMcpClient,
    LazyTtMcpConfig,
    LazyTtMcpError,
    LazyTtMcpTools,
)


MCP_ENV_NAMES = (
    "MCP_ENABLED",
    "MCP_SERVER_NAME",
    "MCP_SERVER_URL",
    "OIDC_TOKEN_URL",
    "OIDC_CLIENT_ID",
    "OIDC_CLIENT_SECRET",
    "MCP_TOOL_ALLOWLIST",
    "MCP_CALL_TIMEOUT_SECONDS",
)


def _config(*, allowlist: tuple[str, ...] = ("area_calculate",)) -> LazyTtMcpConfig:
    return LazyTtMcpConfig(
        server_name="lazytt",
        server_url="http://127.0.0.1:8080/mcp",
        token_url="http://127.0.0.1:8090/token",
        client_id="testagent",
        client_secret="test-secret",
        tool_allowlist=allowlist,
        timeout_seconds=5,
    )


def test_config_is_disabled_when_env_file_does_not_enable_mcp(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    for name in MCP_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MCP_ENV_FILE", str(tmp_path / "missing.env"))

    assert LazyTtMcpConfig.from_env() is None


def test_config_reads_ignored_mcp_env_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    for name in MCP_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    env_file = tmp_path / "lazytt-mcp.env"
    env_file.write_text(
        "\n".join(
            (
                "MCP_ENABLED=true",
                "MCP_SERVER_NAME=lazytt",
                "MCP_SERVER_URL=http://192.168.1.10:8080/mcp",
                "OIDC_TOKEN_URL=http://192.168.1.10:8090/token",
                "OIDC_CLIENT_ID=testagent",
                "OIDC_CLIENT_SECRET=test-secret",
                "MCP_TOOL_ALLOWLIST=area_calculate",
                "MCP_CALL_TIMEOUT_SECONDS=12",
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MCP_ENV_FILE", str(env_file))

    config = LazyTtMcpConfig.from_env()

    assert config is not None
    assert config.server_url == "http://192.168.1.10:8080/mcp"
    assert config.tool_allowlist == ("area_calculate",)
    assert config.timeout_seconds == 12
    assert "test-secret" not in repr(config)


def test_adapter_uses_official_in_process_fake_mcp_server() -> None:
    server = MCPServer("fake-lazytt")

    @server.tool()
    def area_calculate(
        rooms: list[dict[str, Any]],
        deductDoors: bool = True,
        deductWindows: bool = True,
    ) -> dict[str, Any]:
        return {
            "roomCount": len(rooms),
            "deductDoors": deductDoors,
            "deductWindows": deductWindows,
        }

    class InProcessClient(LazyTtMcpClient):
        def _access_token(self) -> str:
            return "fake-token"

        @asynccontextmanager
        async def _connect(self, access_token: str):
            assert access_token == "fake-token"
            async with Client(server) as client:
                yield client

    config = _config()
    tools = LazyTtMcpTools.from_config(
        config,
        client=InProcessClient(config),
    )
    local_name = "mcp__lazytt__area_calculate"

    assert list(tools.tool_definitions) == [local_name]
    assert tools.tool_definitions[local_name]["function"]["name"] == local_name
    assert tools.tool_budgets == {local_name: 1}

    result = tools.tool_handlers[local_name](rooms=[])

    assert result == {
        "success": True,
        "source": "mcp",
        "server": "lazytt",
        "tool": "area_calculate",
        "result": {
            "roomCount": 0,
            "deductDoors": True,
            "deductWindows": True,
        },
    }


def test_adapter_rejects_allowlisted_tool_missing_from_snapshot() -> None:
    server = MCPServer("fake-lazytt")

    class InProcessClient(LazyTtMcpClient):
        def _access_token(self) -> str:
            return "fake-token"

        @asynccontextmanager
        async def _connect(self, access_token: str):
            async with Client(server) as client:
                yield client

    config = _config(allowlist=("area_calculate",))

    with pytest.raises(LazyTtMcpError, match="未发布配置的工具"):
        LazyTtMcpTools.from_config(config, client=InProcessClient(config))


def test_adapter_discovers_selected_material_tools() -> None:
    server = MCPServer("fake-materials")

    @server.tool()
    def area_calculate(rooms: list[dict[str, Any]]) -> dict[str, Any]:
        return {"rooms": rooms}

    @server.tool()
    def paint_calculate(paintAreaM2: float) -> dict[str, Any]:
        return {"paintAreaM2": paintAreaM2}

    @server.tool()
    def tile_calculate(areaM2: float) -> dict[str, Any]:
        return {"areaM2": areaM2}

    @server.tool()
    def flooring_calculate(floorAreaM2: float) -> dict[str, Any]:
        return {"floorAreaM2": floorAreaM2}

    class InProcessClient(LazyTtMcpClient):
        def _access_token(self) -> str:
            return "fake-token"

        @asynccontextmanager
        async def _connect(self, access_token: str):
            async with Client(server) as client:
                yield client

    allowlist = (
        "area_calculate",
        "paint_calculate",
        "tile_calculate",
        "flooring_calculate",
    )
    tools = LazyTtMcpTools.from_config(
        _config(allowlist=allowlist),
        client=InProcessClient(_config(allowlist=allowlist)),
    )

    assert list(tools.tool_definitions) == [
        f"mcp__lazytt__{name}" for name in allowlist
    ]
    assert tools.tool_budgets == {
        f"mcp__lazytt__{name}": 1 for name in allowlist
    }


def test_adapter_infers_missing_enum_type_for_model_tool_schema() -> None:
    remote_tool = type(
        "RemoteTool",
        (),
        {
            "name": "tile_calculate",
            "description": "计算瓷砖采购量。",
            "input_schema": {
                "type": "object",
                "properties": {
                    "layingPattern": {
                        "enum": ["straight", "diagonal", "herringbone"]
                    }
                },
                "required": ["layingPattern"],
                "additionalProperties": False,
            },
        },
    )()

    class FakeClient:
        @staticmethod
        def list_tools():
            return [remote_tool]

        @staticmethod
        def call_tool(_name: str, _arguments: dict[str, Any]) -> dict[str, Any]:
            return {"success": True}

    tools = LazyTtMcpTools.from_config(
        _config(allowlist=("tile_calculate",)),
        client=FakeClient(),
    )

    laying_pattern = tools.tool_definitions[
        "mcp__lazytt__tile_calculate"
    ]["function"]["parameters"]["properties"]["layingPattern"]
    assert laying_pattern["type"] == "string"


def test_access_token_is_cached_without_exposing_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json() -> dict[str, Any]:
            return {"access_token": "short-lived-token", "expires_in": 300}

    class FakeHttpClient:
        def __init__(self, **kwargs: Any) -> None:
            assert kwargs["trust_env"] is False

        def __enter__(self):
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def post(self, _url: str, **kwargs: Any) -> FakeResponse:
            calls.append(kwargs["data"])
            return FakeResponse()

    monkeypatch.setattr(lazytt_mcp.httpx, "Client", FakeHttpClient)
    client = LazyTtMcpClient(_config())

    assert client._access_token() == "short-lived-token"
    assert client._access_token() == "short-lived-token"
    assert len(calls) == 1
    assert calls[0]["client_secret"] == "test-secret"


def test_access_token_refreshes_before_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    now = [100.0]

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json() -> dict[str, Any]:
            return {
                "access_token": f"token-{len(calls)}",
                "expires_in": 50,
            }

    class FakeHttpClient:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def post(self, _url: str, **kwargs: Any) -> FakeResponse:
            calls.append(kwargs["data"])
            return FakeResponse()

    monkeypatch.setattr(lazytt_mcp.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(lazytt_mcp.httpx, "Client", FakeHttpClient)
    client = LazyTtMcpClient(_config())

    assert client._access_token() == "token-1"
    now[0] = 110.0
    assert client._access_token() == "token-1"
    now[0] = 121.0
    assert client._access_token() == "token-2"
    assert len(calls) == 2


@pytest.mark.parametrize("status_code", [401, 403])
def test_token_rejection_is_safe(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
) -> None:
    class FakeResponse:
        def __init__(self) -> None:
            self.status_code = status_code

        @staticmethod
        def json() -> dict[str, Any]:
            return {"error": "test-secret must never be exposed"}

    class FakeHttpClient:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def post(self, _url: str, **_kwargs: Any) -> FakeResponse:
            return FakeResponse()

    monkeypatch.setattr(lazytt_mcp.httpx, "Client", FakeHttpClient)
    client = LazyTtMcpClient(_config())

    with pytest.raises(LazyTtMcpError, match=f"HTTP {status_code}") as caught:
        client._access_token()

    assert "test-secret" not in str(caught.value)


def test_list_tools_timeout_is_normalized() -> None:
    class TimeoutClient(LazyTtMcpClient):
        def _access_token(self) -> str:
            return "fake-token"

        async def _list_tools(self, _access_token: str) -> list[Any]:
            raise TimeoutError("internal-timeout-detail")

    with pytest.raises(
        LazyTtMcpError,
        match="无法读取 LazyTT MCP 工具目录",
    ) as caught:
        TimeoutClient(_config()).list_tools()

    assert "internal-timeout-detail" not in str(caught.value)


def test_tool_transport_failure_is_normalized() -> None:
    class DisconnectedClient(LazyTtMcpClient):
        def _access_token(self) -> str:
            return "fake-token"

        async def _call_tool(
            self,
            _access_token: str,
            _name: str,
            _arguments: dict[str, Any],
        ) -> Any:
            raise OSError("internal-network-detail")

    result = DisconnectedClient(_config()).call_tool("area_calculate", {"rooms": []})

    assert result == {
        "success": False,
        "source": "mcp",
        "server": "lazytt",
        "tool": "area_calculate",
        "error_type": "MCP_TRANSPORT_ERROR",
        "message": "远程 MCP 工具暂时不可用。",
        "retryable": True,
    }
