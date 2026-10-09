import asyncio
import sys

import pytest
from test_runtime import ScriptedModel, quiet, text_response

from agent_client.application.context import validate_pairs
from agent_client.application.runtime import AgentRuntime
from agent_client.application.tools import ToolService
from agent_client.domain.configuration import AppConfig, McpConfig, NamedMcpServer
from agent_client.domain.enums import (
    ApprovalMode,
    JournalEventType,
    McpTransport,
    RunStatus,
    ToolStatus,
)
from agent_client.domain.mcp import McpRequestArguments
from agent_client.domain.models import ModelResponse, ToolCall, ToolContext
from agent_client.domain.protocol import NativeFunctionCall, ProtocolObject
from agent_client.domain.runtime import ContinuationAction, ToolResultCommitted
from agent_client.domain.tools import McpCallArguments, McpReadResourceArguments, ToolName
from agent_client.infrastructure.mcp.manager import McpManager
from agent_client.infrastructure.persistence.store import SessionStore


@pytest.fixture
async def store(tmp_path):
    value = SessionStore(tmp_path / "home")
    await value.open()
    yield value
    await value.close()


def hanging_server(tmp_path, read_only):
    marker = tmp_path / "attempts.txt"
    script = tmp_path / "server.py"
    script.write_text(
        "import asyncio\n"
        "from pathlib import Path\n"
        "from mcp.server.mcpserver import MCPServer\n"
        "from mcp_types import ToolAnnotations\n"
        "server = MCPServer('recovery')\n"
        f"marker = Path({str(marker)!r})\n"
        f"@server.tool(annotations=ToolAnnotations(read_only_hint={read_only!r}))\n"
        "async def inspect(hang: bool) -> str:\n"
        "    count = int(marker.read_text()) if marker.exists() else 0\n"
        "    marker.write_text(str(count + 1))\n"
        "    if hang:\n"
        "        await asyncio.Event().wait()\n"
        "    return 'healthy response'\n"
        "server.run()\n",
        encoding="utf-8",
    )
    return NamedMcpServer(
        name="recovery",
        transport=McpTransport.STDIO,
        command=sys.executable,
        args=[str(script)],
        required=True,
        timeout_seconds=5,
    ), marker


@pytest.mark.asyncio
async def test_timeout_cancels_owner_and_next_independent_request_reconnects(tmp_path):
    config, marker = hanging_server(tmp_path, True)
    manager = McpManager(McpConfig(servers=[config]))
    await manager.start()
    try:
        entry = manager.tool("recovery/inspect")
        assert entry.read_only_hint
        config.timeout_seconds = 0.3
        with pytest.raises((TimeoutError, RuntimeError)):
            await manager.call(
                entry.id,
                McpRequestArguments(
                    arguments=ProtocolObject.model_validate_json('{"hang": true}'),
                    schema_hash=entry.schema_hash,
                ),
            )
        config.timeout_seconds = 5
        result = await asyncio.wait_for(
            manager.call(
                entry.id,
                McpRequestArguments(
                    arguments=ProtocolObject.model_validate_json('{"hang": false}'),
                    schema_hash=entry.schema_hash,
                ),
            ),
            10,
        )
        assert "healthy response" in str(result)
        assert marker.read_text() == "2"
    finally:
        await asyncio.wait_for(manager.close(), 10)


@pytest.mark.asyncio
@pytest.mark.parametrize("read_only", [True, False])
async def test_mcp_timeout_keeps_same_process_conversation_usable(store, tmp_path, read_only):
    server, marker = hanging_server(tmp_path, read_only)
    config = AppConfig(mcp=McpConfig(servers=[server]))
    config.runtime.approval_mode = ApprovalMode.NEVER
    service = ToolService(config, store)
    await service.start()
    session = await store.create_session(tmp_path)
    entry = service.mcp.tool("recovery/inspect")
    arguments = McpCallArguments(
        tool_id=entry.id,
        schema_hash=entry.schema_hash,
        arguments=ProtocolObject.model_validate_json('{"hang": true}'),
    )
    call = ToolCall(id="hung", name=ToolName.CALL_MCP_TOOL, arguments=arguments)
    response = ModelResponse(
        id="call",
        calls=[call],
        output=[
            NativeFunctionCall(
                call_id=call.id, name=call.name, arguments=arguments.model_dump_json()
            )
        ],
    )
    if not read_only:
        second_arguments = McpCallArguments(
            tool_id=entry.id,
            schema_hash=entry.schema_hash,
            arguments=ProtocolObject.model_validate_json('{"hang": false}'),
        )
        response.calls.append(
            ToolCall(id="unsafe-second", name=call.name, arguments=second_arguments)
        )
        response.output.append(
            NativeFunctionCall(
                call_id="unsafe-second",
                name=call.name,
                arguments=second_arguments.model_dump_json(),
            )
        )
    model = ScriptedModel(
        [response, text_response("The request failed"), text_response("Conversation continues")]
    )
    runtime = AgentRuntime(config, store, model, service)
    try:
        server.timeout_seconds = 0.3
        initial = await runtime.run(session, "Inspect", quiet)
        if not read_only:
            assert initial.status == RunStatus.PARTIAL
            plan = await runtime.prepare_continuation(session)
            assert plan.action == ContinuationAction.PREPARED
            assert plan.unresolved_call_ids == ["hung"]
            queued = await runtime.prepare_continuation(session)
            assert queued.action == ContinuationAction.QUEUED
            assert queued.unresolved_call_ids == ["hung"]
            model.responses = iter([text_response("Conversation continues")])
        followup = await runtime.run(session, "Explain the error without retrying", quiet)
        assert followup.status == RunStatus.COMPLETED
        records = await store.read(session)
        results = [
            ToolResultCommitted.model_validate(record.payload).result
            for record in records
            if record.type == JournalEventType.TOOL_RESULT_COMMITTED
        ]
        assert results[0].status == (ToolStatus.FAILED if read_only else ToolStatus.UNKNOWN)
        if not read_only:
            assert results[1].status == ToolStatus.DENIED
        assert marker.read_text() == "1"
        validate_pairs(await runtime.context.load(session, records))
        server.timeout_seconds = 5
        context = ToolContext(
            workspace=tmp_path,
            home=store.home,
            session_id=session,
            run_id="new",
            approval_mode=ApprovalMode.NEVER,
            unresolved_call_ids=[] if read_only else ["hung"],
        )
        modifying = ToolCall(
            id="retry",
            name=ToolName.CALL_MCP_TOOL,
            arguments=McpCallArguments(
                tool_id=entry.id,
                schema_hash=entry.schema_hash,
                arguments=ProtocolObject.model_validate_json('{"hang": false}'),
            ),
        )
        if not read_only:
            result = await service.execute(modifying, context, quiet)
            assert result.status == ToolStatus.DENIED
            assert marker.read_text() == "1"
        else:
            context.unresolved_call_ids = ["other-unknown"]
            context.approval_mode = ApprovalMode.ASK
            denied = await service.execute(modifying, context, quiet)
            assert denied.status == ToolStatus.DENIED
            assert marker.read_text() == "1"
            context.approval_mode = ApprovalMode.NEVER
            healthy = await service.execute(modifying, context, quiet)
            assert healthy.status == ToolStatus.SUCCEEDED
            assert marker.read_text() == "2"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_disconnect_fails_active_and_queued_requests_without_replay(tmp_path):
    config, marker = hanging_server(tmp_path, False)
    manager = McpManager(McpConfig(servers=[config]))
    await manager.start()
    entry = manager.tool("recovery/inspect")
    active = asyncio.create_task(
        manager.call(
            entry.id,
            McpRequestArguments(
                arguments=ProtocolObject.model_validate_json('{"hang": true}'),
                schema_hash=entry.schema_hash,
            ),
        )
    )
    queued = None
    try:
        for _ in range(100):
            if marker.exists():
                break
            await asyncio.sleep(0.01)
        assert marker.exists()
        queued = asyncio.create_task(
            manager.call(
                entry.id,
                McpRequestArguments(
                    arguments=ProtocolObject.model_validate_json('{"hang": false}'),
                    schema_hash=entry.schema_hash,
                ),
            )
        )
        await asyncio.sleep(0)
        await asyncio.wait_for(manager.close(), 3)
        outcomes = await asyncio.wait_for(asyncio.gather(active, queued, return_exceptions=True), 1)
        assert isinstance(outcomes[0], RuntimeError)
        assert isinstance(outcomes[1], ValueError)
        assert marker.read_text() == "1"
    finally:
        active.cancel()
        if queued is not None:
            queued.cancel()
        await manager.close()


@pytest.mark.asyncio
async def test_transport_eof_reconnects_for_next_request_without_replay(tmp_path):
    config, marker = hanging_server(tmp_path, False)
    script = tmp_path / "server.py"
    script.write_text(
        script.read_text(encoding="utf-8")
        .replace("import asyncio\n", "import asyncio\nimport os\n")
        .replace("await asyncio.Event().wait()", "os._exit(1)"),
        encoding="utf-8",
    )
    manager = McpManager(McpConfig(servers=[config]))
    await manager.start()
    try:
        entry = manager.tool("recovery/inspect")
        with pytest.raises(RuntimeError):
            await asyncio.wait_for(
                manager.call(
                    entry.id,
                    McpRequestArguments(
                        arguments=ProtocolObject.model_validate_json('{"hang": true}'),
                        schema_hash=entry.schema_hash,
                    ),
                ),
                3,
            )
        await asyncio.sleep(0)
        result = await asyncio.wait_for(
            manager.call(
                entry.id,
                McpRequestArguments(
                    arguments=ProtocolObject.model_validate_json('{"hang": false}'),
                    schema_hash=entry.schema_hash,
                ),
            ),
            10,
        )
        assert "healthy response" in str(result)
        assert marker.read_text() == "2"
    finally:
        await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,server_name",
    [
        (ToolName.CALL_MCP_TOOL, "unavailable"),
        (ToolName.READ_MCP_RESOURCE, "missing"),
        (ToolName.READ_MCP_RESOURCE, "unavailable"),
    ],
)
async def test_unavailable_mcp_metadata_fails_without_breaking_next_message(
    store, tmp_path, name, server_name
):
    config = AppConfig(
        mcp=McpConfig(
            servers=[
                NamedMcpServer(
                    name="unavailable",
                    transport=McpTransport.STDIO,
                    command=sys.executable,
                    args=["-c", "raise SystemExit(1)"],
                    timeout_seconds=1,
                )
            ]
        )
    )
    config.runtime.approval_mode = ApprovalMode.NEVER
    service = ToolService(config, store)
    await service.start()
    session = await store.create_session(tmp_path)
    match name:
        case ToolName.CALL_MCP_TOOL:
            arguments = McpCallArguments(
                tool_id=server_name + "/cached", schema_hash="a" * 64, arguments=ProtocolObject()
            )
        case ToolName.READ_MCP_RESOURCE:
            arguments = McpReadResourceArguments(server=server_name, uri="test://cached")
    call = ToolCall(id="unavailable-call", name=name, arguments=arguments)
    response = ModelResponse(
        id="unavailable",
        calls=[call],
        output=[
            NativeFunctionCall(
                call_id=call.id, name=call.name, arguments=arguments.model_dump_json()
            )
        ],
    )
    model = ScriptedModel(
        [response, text_response("The server is unavailable"), text_response("New message works")]
    )
    runtime = AgentRuntime(config, store, model, service)
    try:
        assert (
            await runtime.run(session, "Inspect unavailable data", quiet)
        ).status == RunStatus.COMPLETED
        records = await store.read(session)
        result = next(
            ToolResultCommitted.model_validate(record.payload).result
            for record in records
            if record.type == JournalEventType.TOOL_RESULT_COMMITTED
        )
        assert result.status == ToolStatus.FAILED
        assert not runtime._unresolved_unknown(records)
        assert (
            await runtime.run(session, "Discuss another topic", quiet)
        ).status == RunStatus.COMPLETED
        assert len(model.requests) == 3
        validate_pairs(await runtime.context.load(session, await store.read(session)))
    finally:
        await service.close()
