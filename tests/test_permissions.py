import hashlib
import os
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_client.application.tools import ToolService
from agent_client.domain.configuration import AppConfig, RuntimeConfig
from agent_client.domain.enums import ApprovalMode, ToolStatus
from agent_client.domain.events import RuntimeEvent
from agent_client.domain.mcp import McpToolEntry, McpToolResult
from agent_client.domain.models import ApprovalRequest, ToolCall, ToolContext
from agent_client.domain.protocol import ProtocolObject
from agent_client.domain.tools import ToolName
from agent_client.domain.workspace import FileVersion
from agent_client.presentation.cli import parser


class ArtifactStore:
    def __init__(self, home: Path):
        self.home = home

    async def put_artifact(self, session_id: str, content: str) -> str:
        return hashlib.sha256(content.encode()).hexdigest()

    async def put_artifact_file(self, session_id: str, source: Path) -> str:
        return hashlib.sha256(source.read_bytes()).hexdigest()


def write_call(path: str = "created.txt") -> ToolCall:
    return ToolCall(
        id="write",
        name=ToolName.WRITE_FILE,
        arguments={"path": path, "content": "created", "before_hash": FileVersion.MISSING},
    )


@pytest.mark.asyncio
async def test_never_executes_write_command_and_remote_without_approval(tmp_path):
    service = ToolService(AppConfig(), ArtifactStore(tmp_path))
    context = ToolContext(
        workspace=tmp_path,
        home=tmp_path,
        session_id="session",
        run_id="run",
        approval_mode=ApprovalMode.NEVER,
    )
    remote_calls = []

    async def emit(event: RuntimeEvent) -> None:
        pass

    async def approve(request: ApprovalRequest) -> bool:
        pytest.fail("Never mode must not request approval")

    async def remote(tool_id, request):
        remote_calls.append(tool_id)
        return McpToolResult(content=[{"type": "text", "text": "done"}])

    service.mcp.call = remote
    service.mcp.tools = [
        McpToolEntry(
            id="s/tool", server="s", name="tool", parameters=ProtocolObject(), schema_hash="a" * 64
        )
    ]
    result = await service.execute(write_call(), context, emit, approve)
    assert result.status == ToolStatus.SUCCEEDED
    assert (tmp_path / "created.txt").read_text() == "created"
    command = (
        f'& "{sys.executable}" -c "print(123)"'
        if os.name == "nt"
        else f'"{sys.executable}" -c "print(123)"'
    )
    result = await service.execute(
        ToolCall(id="command", name=ToolName.RUN_COMMAND, arguments={"command": command}),
        context,
        emit,
        approve,
    )
    assert result.status == ToolStatus.SUCCEEDED
    assert "123" in result.content.stdout
    result = await service.execute(
        ToolCall(
            id="remote",
            name=ToolName.CALL_MCP_TOOL,
            arguments={"tool_id": "s/tool", "schema_hash": "a" * 64, "arguments": {}},
        ),
        context,
        emit,
        approve,
    )
    assert result.status == ToolStatus.SUCCEEDED
    assert remote_calls == ["s/tool"]
    await service.close()


@pytest.mark.asyncio
async def test_read_only_denies_side_effects_even_with_allow_flags(tmp_path):
    service = ToolService(AppConfig(), ArtifactStore(tmp_path))
    context = ToolContext(
        workspace=tmp_path,
        home=tmp_path,
        session_id="session",
        run_id="run",
        approval_mode=ApprovalMode.READ_ONLY,
        allow_write=True,
        allow_commands=True,
    )

    async def emit(event: RuntimeEvent) -> None:
        pytest.fail("Denied calls must not dispatch")

    async def approve(request: ApprovalRequest) -> bool:
        pytest.fail("Read-only mode must not request approval")

    calls = [
        write_call(),
        ToolCall(id="command", name=ToolName.RUN_COMMAND, arguments={"command": "echo 123"}),
        ToolCall(
            id="remote",
            name=ToolName.CALL_MCP_TOOL,
            arguments={"tool_id": "s/tool", "schema_hash": "a" * 64, "arguments": {}},
        ),
    ]
    for call in calls:
        result = await service.execute(call, context, emit, approve)
        assert result.status == ToolStatus.DENIED
    assert not (tmp_path / "created.txt").exists()
    assert not service.commands
    await service.close()


@pytest.mark.asyncio
async def test_never_retains_workspace_boundary(tmp_path):
    service = ToolService(AppConfig(), ArtifactStore(tmp_path))
    context = ToolContext(
        workspace=tmp_path,
        home=tmp_path,
        session_id="s",
        run_id="r",
        approval_mode=ApprovalMode.NEVER,
    )

    async def emit(event: RuntimeEvent) -> None:
        pass

    result = await service.execute(write_call("../escaped.txt"), context, emit)
    assert result.status == ToolStatus.FAILED
    assert not (tmp_path.parent / "escaped.txt").exists()
    await service.close()


def test_approval_mode_validates_configuration_and_cli():
    assert RuntimeConfig().approval_mode == ApprovalMode.ASK
    assert (
        parser().parse_args(["--approval-mode", "never", "run", "hello"]).approval_mode == "never"
    )
    with pytest.raises(ValidationError):
        RuntimeConfig(approval_mode="invalid")
