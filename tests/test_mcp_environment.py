import sys
import threading
from contextlib import asynccontextmanager

import pytest
from mcp_types import ToolAnnotations
from pydantic import Field

from agent_client.domain.base import Contract
from agent_client.domain.configuration import McpConfig, McpServerConfig, NamedMcpServer
from agent_client.domain.enums import McpTransport
from agent_client.domain.mcp import McpConnectionStatus, McpContent, McpContentType, McpToolResult
from agent_client.domain.protocol import ProtocolObject
from agent_client.infrastructure.mcp import environment as environment_module
from agent_client.infrastructure.mcp import manager as manager_module
from agent_client.infrastructure.mcp.environment import MissingMcpEnvironment, resolve_environment
from agent_client.infrastructure.mcp.manager import McpManager


class RegistryKey:
    def __init__(self, root: int):
        self.root = root

    def __enter__(self):
        return self

    def __exit__(self, *arguments):
        pass


class Registry:
    HKEY_CURRENT_USER = 1
    HKEY_LOCAL_MACHINE = 2
    REG_SZ = 1
    REG_EXPAND_SZ = 2

    def __init__(self):
        self.values: dict[tuple[int, str], str] = {}
        self.queries: list[tuple[int, str]] = []

    def OpenKey(self, root, path):
        return RegistryKey(root)

    def QueryValueEx(self, key, reference):
        self.queries.append((key.root, reference))
        if (key.root, reference) not in self.values:
            raise FileNotFoundError(reference)
        return self.values[key.root, reference], self.REG_SZ


def test_windows_lookup_queries_only_requested_user_then_machine_variable(monkeypatch):
    registry = Registry()
    registry.values[1, "TEST_MCP_VALUE"] = "user-value"
    registry.values[2, "TEST_MCP_VALUE"] = "machine-value"
    monkeypatch.setitem(sys.modules, "winreg", registry)
    assert environment_module.windows_environment("TEST_MCP_VALUE") == "user-value"
    assert registry.queries == [(1, "TEST_MCP_VALUE")]
    registry.values.pop((1, "TEST_MCP_VALUE"))
    registry.queries.clear()
    assert environment_module.windows_environment("TEST_MCP_VALUE") == "machine-value"
    assert registry.queries == [(1, "TEST_MCP_VALUE"), (2, "TEST_MCP_VALUE")]
    registry.values[1, "TEST_MCP_VALUE"] = ""
    registry.queries.clear()
    assert environment_module.windows_environment("TEST_MCP_VALUE") == ""
    assert registry.queries == [(1, "TEST_MCP_VALUE")]


async def test_process_value_wins_and_empty_explicit_process_value_never_falls_back(monkeypatch):
    def forbidden(reference):
        raise AssertionError("An explicit process value must not query persistent environment")

    monkeypatch.setattr(environment_module, "windows_environment", forbidden)
    monkeypatch.setenv("TEST_MCP_VALUE", "process-value")
    assert (await resolve_environment("TEST_MCP_VALUE")).get_secret_value() == "process-value"
    monkeypatch.setenv("TEST_MCP_VALUE", "")
    with pytest.raises(MissingMcpEnvironment, match="TEST_MCP_VALUE"):
        await resolve_environment("TEST_MCP_VALUE")


async def test_persistent_lookup_runs_outside_the_event_loop_without_importing_environment(
    monkeypatch,
):
    monkeypatch.delenv("TEST_MCP_VALUE", raising=False)
    main_thread = threading.get_ident()
    queried: list[str] = []

    def persistent(reference):
        assert threading.get_ident() != main_thread
        queried.append(reference)
        return "persistent-value"

    monkeypatch.setattr(environment_module, "windows_environment", persistent)
    value = await resolve_environment("TEST_MCP_VALUE")
    assert value.get_secret_value() == "persistent-value"
    assert "persistent-value" not in repr(value)
    assert queried == ["TEST_MCP_VALUE"]
    assert "TEST_MCP_VALUE" not in environment_module.os.environ


class ToolFixture(Contract):
    name: str = "echo"
    description: str
    annotations: ToolAnnotations | None = None
    input_schema: ProtocolObject = Field(
        default_factory=lambda: ProtocolObject('{"type":"object"}')
    )


class DirectoryFixture(Contract):
    tools: list[ToolFixture]
    next_cursor: str | None = None


@pytest.mark.parametrize("transport", list(McpTransport))
async def test_persistent_credentials_reach_transport_and_are_redacted_from_results(
    monkeypatch, transport
):
    secret = 'persistent-token-"quoted"' + ("🙂" if transport == McpTransport.STDIO else "")
    monkeypatch.delenv("TEST_MCP_TOKEN", raising=False)
    monkeypatch.setattr(environment_module, "windows_environment", lambda reference: secret)
    config = McpServerConfig(
        transport=transport,
        command="fixture" if transport == McpTransport.STDIO else None,
        url="https://mcp.test" if transport == McpTransport.STREAMABLE_HTTP else None,
        env={"TOKEN": "TEST_MCP_TOKEN"} if transport == McpTransport.STDIO else {},
        token_env="TEST_MCP_TOKEN" if transport == McpTransport.STREAMABLE_HTTP else None,
        required=True,
    )

    @asynccontextmanager
    async def http_transport(url, http_client):
        assert url == "https://mcp.test"
        assert http_client.headers["Authorization"] == f"Bearer {secret}"
        yield object(), object()

    class ClientFixture:
        protocol_version = "2025-11-25"

        def __init__(self, server, **options):
            self.server = server
            if transport == McpTransport.STDIO:
                assert server.env == {"TOKEN": secret}

        async def __aenter__(self):
            if transport == McpTransport.STREAMABLE_HTTP:
                await self.server.__aenter__()
            return self

        async def __aexit__(self, *arguments):
            if transport == McpTransport.STREAMABLE_HTTP:
                await self.server.__aexit__(*arguments)

        async def list_tools(self, **arguments):
            return DirectoryFixture(tools=[ToolFixture(description=secret)])

    monkeypatch.setattr(manager_module, "Client", ClientFixture)
    monkeypatch.setattr(manager_module, "streamable_http_client", http_transport)
    manager = McpManager(McpConfig(servers=[NamedMcpServer(name="fixture", **config.model_dump())]))
    await manager.start()
    try:
        assert manager.status[0].status == McpConnectionStatus.CONNECTED
        assert manager.search("")[0].description == "[REDACTED]"
        result = McpToolResult(content=[McpContent(type=McpContentType.TEXT, text=secret)])
        monkeypatch.setenv("TEST_MCP_TOKEN", "replacement-process-token")
        sanitized = manager._sanitize(result, McpToolResult, config)
        assert sanitized.content[0].text == "[REDACTED]"
        assert secret not in manager.status[0].model_dump_json()
    finally:
        await manager.close()
    assert all(not connection.secrets for connection in manager.connections)


@pytest.mark.parametrize("required", [False, True])
async def test_missing_credential_reports_only_the_configured_variable_name(monkeypatch, required):
    monkeypatch.delenv("TEST_MCP_MISSING", raising=False)
    monkeypatch.setattr(environment_module, "windows_environment", lambda reference: None)
    config = McpConfig(
        servers=[
            NamedMcpServer(
                name="fixture",
                transport=McpTransport.STREAMABLE_HTTP,
                url="https://mcp.test",
                token_env="TEST_MCP_MISSING",
                required=required,
            )
        ]
    )
    manager = McpManager(config)
    if required:
        with pytest.raises(RuntimeError, match="TEST_MCP_MISSING"):
            await manager.start()
    else:
        await manager.start()
    assert "TEST_MCP_MISSING" in manager.status[0].error
    assert manager.search("") == []
    await manager.close()
