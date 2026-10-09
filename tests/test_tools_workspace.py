import asyncio
import hashlib
import os
import sys
from pathlib import Path

import pytest

from agent_client.application.instructions import scoped_path
from agent_client.application.tools import ToolService
from agent_client.domain.configuration import AppConfig
from agent_client.domain.enums import ToolStatus
from agent_client.domain.events import RuntimeEvent
from agent_client.domain.mcp import McpRequestArguments, McpToolEntry, McpToolResult
from agent_client.domain.models import ApprovalRequest, ToolCall, ToolContext
from agent_client.domain.protocol import ProtocolObject
from agent_client.domain.tools import ToolName
from agent_client.domain.workspace import FileVersion, PlatformKind
from agent_client.infrastructure.workspace.files import write_file
from agent_client.infrastructure.workspace.process import ProcessRunner


class MemoryStore:
    def __init__(self, home: Path):
        self.home = home
        self.artifacts: dict[str, str] = {}

    async def put_artifact(self, session_id: str, content: str) -> str:
        identity = hashlib.sha256(content.encode()).hexdigest()
        self.artifacts[identity] = content
        return identity

    async def put_artifact_file(self, session_id: str, path: Path) -> str:
        return await self.put_artifact(session_id, path.read_text(encoding="utf-8"))

    async def read_artifact(self, session_id: str, artifact_id: str) -> str:
        return self.artifacts[artifact_id]


def test_workspace_path_cannot_escape(tmp_path):
    with pytest.raises(ValueError, match="escapes"):
        scoped_path(tmp_path, "../outside.txt")
    outside = tmp_path.parent / "outside_target"
    outside.mkdir(exist_ok=True)
    try:
        (tmp_path / "link").symlink_to(outside, target_is_directory=True)
    except OSError:
        return
    with pytest.raises(ValueError, match="escapes"):
        scoped_path(tmp_path, "link/file.txt")


def test_file_conflict_preserves_external_edit(tmp_path):
    file = tmp_path / "sample.txt"
    file.write_text("external", encoding="utf-8")
    with pytest.raises(ValueError, match="conflict"):
        write_file(tmp_path, "sample.txt", "replacement", hashlib.sha256(b"original").hexdigest())
    assert file.read_text() == "external"


@pytest.mark.asyncio
async def test_process_drain_timeout_and_failure(tmp_path):
    runner = ProcessRunner()
    result = await runner.run(
        [sys.executable, "-c", "import sys;print('x'*100000);sys.exit(1)"], tmp_path, limit=100
    )
    assert result.exit_code == 1 and result.truncated
    result = await runner.run(
        [sys.executable, "-c", "import time;time.sleep(20)"], tmp_path, timeout=0.1
    )
    assert result.timed_out


@pytest.mark.asyncio
async def test_dispatch_failure_never_writes(tmp_path):
    class Store:
        home = tmp_path

    service = ToolService(AppConfig(), Store())
    context = ToolContext(
        workspace=tmp_path, home=tmp_path, session_id="s", run_id="r", allow_write=True
    )

    async def emit(event: RuntimeEvent) -> None:
        raise RuntimeError("journal unavailable")

    result = await service.execute(
        ToolCall(
            id="c",
            name=ToolName.WRITE_FILE,
            arguments={"path": "new.txt", "content": "hello", "before_hash": FileVersion.MISSING},
        ),
        context,
        emit,
    )
    assert result.is_error
    assert not (tmp_path / "new.txt").exists()


@pytest.mark.asyncio
async def test_approval_denial_never_dispatches(tmp_path):
    class Store:
        home = tmp_path

    service = ToolService(AppConfig(), Store())
    events = []

    async def emit(event: RuntimeEvent) -> None:
        events.append(event)

    async def approve(request: ApprovalRequest) -> bool:
        return False

    context = ToolContext(workspace=tmp_path, home=tmp_path, session_id="s", run_id="r")
    result = await service.execute(
        ToolCall(
            id="c",
            name=ToolName.WRITE_FILE,
            arguments={"path": "new.txt", "content": "hello", "before_hash": FileVersion.MISSING},
        ),
        context,
        emit,
        approve,
    )
    assert result.status == ToolStatus.DENIED and not events


@pytest.mark.asyncio
async def test_command_handle_stop_and_owner(tmp_path):
    store = MemoryStore(tmp_path)
    service = ToolService(AppConfig(), store)
    context = ToolContext(
        workspace=tmp_path, home=tmp_path, session_id="s", run_id="r", allow_commands=True
    )

    async def emit(event: RuntimeEvent) -> None:
        pass

    command = (
        f'& "{sys.executable}" -c "import time;time.sleep(30)"'
        if PlatformKind(os.name) == PlatformKind.WINDOWS
        else f'"{sys.executable}" -c "import time;time.sleep(30)"'
    )
    result = await service.execute(
        ToolCall(
            id="a", name=ToolName.RUN_COMMAND, arguments={"command": command, "yield_seconds": 0.2}
        ),
        context,
        emit,
    )
    handle = result.content.process_handle
    assert result.content.running
    foreign = context.model_copy(update={"session_id": "other"})
    rejected = await service.execute(
        ToolCall(id="p", name=ToolName.POLL_COMMAND, arguments={"process_handle": handle}),
        foreign,
        emit,
    )
    assert rejected.is_error
    stopped = await service.execute(
        ToolCall(id="b", name=ToolName.STOP_COMMAND, arguments={"process_handle": handle}),
        context,
        emit,
    )
    assert stopped.content.cancelled and stopped.status == ToolStatus.CANCELLED
    assert stopped.content.exit_code is not None
    assert not stopped.content.side_effect_result_unknown
    await service.close()


@pytest.mark.asyncio
async def test_command_artifact_keeps_output_beyond_preview(tmp_path):
    store = MemoryStore(tmp_path)
    service = ToolService(AppConfig(), store)
    context = ToolContext(
        workspace=tmp_path, home=tmp_path, session_id="s", run_id="r", allow_commands=True
    )

    async def emit(event: RuntimeEvent) -> None:
        pass

    command = (
        f'& "{sys.executable}" -c "print(\'x\'*1200000)"'
        if PlatformKind(os.name) == PlatformKind.WINDOWS
        else f'"{sys.executable}" -c "print(\'x\'*1200000)"'
    )
    result = await service.execute(
        ToolCall(id="a", name=ToolName.RUN_COMMAND, arguments={"command": command}), context, emit
    )
    assert result.content.exit_code == 0
    assert len(store.artifacts[result.content.stdout_artifact_id]) > 1100000
    assert not list((tmp_path / "temporary" / "commands").iterdir())
    await service.close()


@pytest.mark.asyncio
async def test_directory_rules_are_returned_before_first_write(tmp_path):
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "AGENTS.md").write_text("Use specific rules", encoding="utf-8")
    service = ToolService(AppConfig(), MemoryStore(tmp_path))
    context = ToolContext(
        workspace=tmp_path, home=tmp_path, session_id="s", run_id="r", allow_write=True
    )
    events = []

    async def emit(event: RuntimeEvent) -> None:
        events.append(event)

    call = ToolCall(
        id="w",
        name=ToolName.WRITE_FILE,
        arguments={"path": "nested/a.txt", "content": "hello", "before_hash": FileVersion.MISSING},
    )
    result = await service.execute(call, context, emit)
    assert result.is_error and "Use specific rules" in result.content.applicable_instructions
    assert not events and not (tmp_path / "nested" / "a.txt").exists()
    result = await service.execute(call, context, emit)
    assert not result.is_error and (tmp_path / "nested" / "a.txt").read_text() == "hello"


@pytest.mark.asyncio
async def test_timeout_kills_descendant_after_parent_exits(tmp_path):
    marker = tmp_path / "escaped.txt"
    child = f"import time,pathlib;time.sleep(2);pathlib.Path({str(marker)!r}).write_text('escaped')"
    parent = f"import subprocess,sys;subprocess.Popen([sys.executable,'-c',{child!r}])"
    result = await ProcessRunner().run([sys.executable, "-c", parent], tmp_path, timeout=0.3)
    assert result.timed_out
    await asyncio.sleep(2)
    assert not marker.exists()


@pytest.mark.asyncio
async def test_rg_unicode_literal_no_match_and_invalid_path(tmp_path):
    (tmp_path / "file.txt").write_text("\u4f60\u597d.*[literal]\n", encoding="utf-8")
    service = ToolService(AppConfig(), MemoryStore(tmp_path))
    context = ToolContext(workspace=tmp_path, home=tmp_path, session_id="s", run_id="r")

    async def emit(event: RuntimeEvent) -> None:
        pass

    result = await service.execute(
        ToolCall(id="s", name=ToolName.SEARCH_TEXT, arguments={"query": "\u4f60\u597d.*[literal]"}),
        context,
        emit,
    )
    assert not result.is_error and len(result.content.matches) == 1
    absent = await service.execute(
        ToolCall(id="a", name=ToolName.SEARCH_TEXT, arguments={"query": "absent"}), context, emit
    )
    assert not absent.is_error and absent.content.exit_code == 1
    failed = await service.execute(
        ToolCall(
            id="b", name=ToolName.SEARCH_TEXT, arguments={"query": "absent", "path": "nonexistent"}
        ),
        context,
        emit,
    )
    assert failed.is_error and failed.content.exit_code == 2


@pytest.mark.asyncio
async def test_remote_timeout_and_post_write_artifact_failure_are_unknown(tmp_path):
    service = ToolService(AppConfig(), MemoryStore(tmp_path))
    context = ToolContext(
        workspace=tmp_path, home=tmp_path, session_id="s", run_id="r", allow_write=True
    )

    async def emit(event: RuntimeEvent) -> None:
        pass

    async def approve(request: ApprovalRequest) -> bool:
        return True

    async def timeout(identity: str, arguments: McpRequestArguments) -> McpToolResult:
        raise TimeoutError

    service.mcp.call = timeout
    service.mcp.tools = [
        McpToolEntry(
            id="s/tool", server="s", name="tool", parameters=ProtocolObject(), schema_hash="a" * 64
        )
    ]
    result = await service.execute(
        ToolCall(
            id="m",
            name=ToolName.CALL_MCP_TOOL,
            arguments={"tool_id": "s/tool", "schema_hash": "a" * 64, "arguments": {}},
        ),
        context,
        emit,
        approve,
    )
    assert result.status == ToolStatus.UNKNOWN

    async def disk_failure(session_id: str, content: str) -> str:
        raise OSError("disk unavailable")

    service.store.put_artifact = disk_failure
    result = await service.execute(
        ToolCall(
            id="w",
            name=ToolName.WRITE_FILE,
            arguments={
                "path": "created.txt",
                "content": "created",
                "before_hash": FileVersion.MISSING,
            },
        ),
        context,
        emit,
    )
    assert result.status == ToolStatus.UNKNOWN
    assert (tmp_path / "created.txt").read_text() == "created"
