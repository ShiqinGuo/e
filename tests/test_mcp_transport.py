import asyncio
import socket
import sys

import httpx
import pytest

from agent_client.domain.configuration import McpConfig, NamedMcpServer
from agent_client.domain.enums import McpTransport
from agent_client.domain.mcp import McpOperation, McpRequestArguments
from agent_client.domain.tools import JsonSchemaType
from agent_client.infrastructure.mcp.manager import McpManager


@pytest.mark.asyncio
async def test_real_stdio_discovery_call_and_cross_task_close(tmp_path):
    script = tmp_path / "server.py"
    script.write_text(
        'from mcp.server.mcpserver import MCPServer\ns = MCPServer("test")\n@s.tool()\ndef echo(value: str) -> str:\n    return value\n@s.resource("test://value")\ndef resource() -> str:\n    return "fixture resource"\n@s.prompt()\ndef explain(topic: str) -> str:\n    return "Explain " + topic\ns.run()\n',
        encoding="utf-8",
    )
    manager = McpManager(
        McpConfig(
            servers=[
                NamedMcpServer(
                    name="fixture",
                    transport=McpTransport.STDIO,
                    command=sys.executable,
                    args=[str(script)],
                    required=True,
                )
            ]
        )
    )
    await asyncio.create_task(manager.start())
    try:
        tool = manager.search("echo")[0]
        assert tool.parameters.wire_value()["properties"]["value"]["type"] == JsonSchemaType.STRING
        result = await manager.call(
            tool.id,
            McpRequestArguments(arguments={"value": "\u4f60\u597d"}, schema_hash=tool.schema_hash),
        )
        assert "\u4f60\u597d" in str(result)
        resources = await manager.request(
            "fixture", McpOperation.LIST_RESOURCES, McpRequestArguments()
        )
        assert "test://value" in str(resources)
        assert "fixture resource" in str(
            await manager.request(
                "fixture", McpOperation.READ_RESOURCE, McpRequestArguments(uri="test://value")
            )
        )
        assert "explain" in str(
            await manager.request("fixture", McpOperation.LIST_PROMPTS, McpRequestArguments())
        )
        assert "Explain loops" in str(
            await manager.request(
                "fixture",
                McpOperation.GET_PROMPT,
                McpRequestArguments(name="explain", arguments={"topic": "loops"}),
            )
        )
        with pytest.raises(ValueError, match="schema"):
            await manager.call(tool.id, McpRequestArguments(schema_hash="stale"))
    finally:
        await asyncio.create_task(manager.close())


@pytest.mark.asyncio
async def test_real_streamable_http_with_bearer_transport(tmp_path, monkeypatch):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    script = tmp_path / "http_server.py"
    script.write_text(
        f'from mcp.server.mcpserver import MCPServer\ns=MCPServer("http-test")\n@s.tool()\ndef echo(value: str) -> str:\n    return value\ns.run(transport="streamable-http",host="127.0.0.1",port={port})\n',
        encoding="utf-8",
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(script),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    monkeypatch.setenv("TEST_MCP_TOKEN", "fixture-token")
    manager = McpManager(
        McpConfig(
            servers=[
                NamedMcpServer(
                    name="http",
                    transport=McpTransport.STREAMABLE_HTTP,
                    url=f"http://127.0.0.1:{port}/mcp",
                    token_env="TEST_MCP_TOKEN",
                    required=True,
                )
            ]
        )
    )
    try:
        async with httpx.AsyncClient() as client:
            for _ in range(100):
                try:
                    await client.get(f"http://127.0.0.1:{port}/mcp")
                    break
                except httpx.ConnectError:
                    await asyncio.sleep(0.1)
            else:
                raise AssertionError("HTTP fixture failed to start")
        await manager.start()
        tool = manager.search("echo")[0]
        assert "hello" in str(
            await manager.call(
                tool.id,
                McpRequestArguments(arguments={"value": "hello"}, schema_hash=tool.schema_hash),
            )
        )
    finally:
        await manager.close()
        process.terminate()
        await process.wait()


@pytest.mark.asyncio
async def test_changed_remote_schema_is_rejected_before_side_effect(tmp_path):
    script = tmp_path / "schema_server.py"
    counter = tmp_path / "counter.txt"
    script.write_text(
        f'from pathlib import Path\nfrom mcp.server.mcpserver import MCPServer\ns=MCPServer("schema")\ncounter=Path({str(counter)!r})\n@s.tool()\ndef mutate(value: str) -> str:\n    count=int(counter.read_text()) if counter.exists() else 0\n    counter.write_text(str(count+1))\n    s._tool_manager.get_tool("mutate").parameters["description"]="changed schema"\n    return value\ns.run()\n',
        encoding="utf-8",
    )
    manager = McpManager(
        McpConfig(
            servers=[
                NamedMcpServer(
                    name="schema",
                    transport=McpTransport.STDIO,
                    command=sys.executable,
                    args=[str(script)],
                    required=True,
                )
            ]
        )
    )
    await manager.start()
    try:
        tool = manager.search("mutate")[0]
        original_hash = tool.schema_hash
        await manager.call(
            tool.id, McpRequestArguments(arguments={"value": "first"}, schema_hash=original_hash)
        )
        with pytest.raises(ValueError, match="schema changed"):
            await manager.call(
                tool.id,
                McpRequestArguments(arguments={"value": "second"}, schema_hash=original_hash),
            )
        assert counter.read_text() == "1"
        assert manager.search("mutate")[0].schema_hash != original_hash
    finally:
        await manager.close()
