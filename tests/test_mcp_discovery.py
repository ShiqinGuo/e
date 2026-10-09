import asyncio

import pytest

from agent_client.application.tools import ToolService
from agent_client.domain.configuration import AppConfig, NamedMcpServer
from agent_client.domain.enums import McpTransport
from agent_client.domain.events import RuntimeEvent
from agent_client.domain.mcp import McpConnectionStatus, McpNamedStatus, McpToolEntry
from agent_client.domain.models import ToolCall, ToolContext
from agent_client.domain.tools import ToolName
from agent_client.infrastructure.mcp.manager import ServerConnection
from agent_client.infrastructure.persistence.store import SessionStore


@pytest.mark.parametrize("connected", [False, True])
async def test_mcp_search_distinguishes_connection_failure_from_no_match(tmp_path, connected):
    store = await SessionStore(tmp_path / "home").open()
    service = ToolService(AppConfig(), store)
    connection = ServerConnection(
        name="mining-news",
        config=NamedMcpServer(
            name="mining-news", transport=McpTransport.STREAMABLE_HTTP, url="http://mcp.test"
        ),
        queue=asyncio.Queue(),
    )
    service.mcp.connections.append(connection)
    connection.status = McpNamedStatus(
        name="mining-news",
        status=McpConnectionStatus.CONNECTED if connected else McpConnectionStatus.DISCONNECTED,
        protocol_version="2026-07-28" if connected else None,
        error=None if connected else "Missing environment variable: MINING_SERVICE_TOKEN",
    )
    if connected:
        service.mcp.tools.append(
            McpToolEntry(
                id="mining-news/search",
                server="mining-news",
                name="search",
                description="Search mining news",
                parameters={"type": "object"},
                schema_hash="fixture",
            )
        )
    identity = await store.create_session(tmp_path)
    context = ToolContext(workspace=tmp_path, home=store.home, session_id=identity, run_id="run")

    async def emit(event: RuntimeEvent):
        pass

    try:
        missing = await service.execute(
            ToolCall(
                id="missing",
                name=ToolName.SEARCH_MCP_TOOLS,
                arguments={"query": "current news prices reserves unavailable keyword"},
            ),
            context,
            emit,
        )
        assert missing.content.tools == []
        expected = McpConnectionStatus.CONNECTED if connected else McpConnectionStatus.DISCONNECTED
        assert missing.content.servers[0].status == expected
        assert "empty query" in missing.content.notice
        listed = await service.execute(
            ToolCall(id="list", name=ToolName.SEARCH_MCP_TOOLS, arguments={"query": ""}),
            context,
            emit,
        )
        assert len(listed.content.tools) == int(connected)
        instructions = await service.instructions(tmp_path)
        assert "mining-news" in instructions
        assert expected.value in instructions
        if not connected:
            assert "MINING_SERVICE_TOKEN" in instructions
    finally:
        await service.close()
        await store.close()
