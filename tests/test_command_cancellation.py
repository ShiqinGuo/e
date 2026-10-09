import asyncio
import os
import sys
from pathlib import Path

import pytest

from agent_client.application.context import validate_pairs
from agent_client.application.runtime import AgentRuntime
from agent_client.application.tools import ToolService, WorkspaceLock
from agent_client.domain.configuration import AppConfig
from agent_client.domain.enums import (
    ApprovalMode,
    JournalEventType,
    ModelEventKind,
    NativeItemType,
    RunStatus,
    RuntimeEventKind,
    StopReason,
    ToolStatus,
)
from agent_client.domain.events import RuntimeEvent
from agent_client.domain.models import (
    ModelEvent,
    ModelResponse,
    ToolCall,
    ToolContext,
)
from agent_client.domain.runtime import ContinuationAction, ToolResultCommitted
from agent_client.domain.tools import ToolName
from agent_client.infrastructure.persistence.maintenance import ProjectionMaintenance
from agent_client.infrastructure.persistence.store import SessionStore
from agent_client.infrastructure.workspace.process import ProcessRunner


class CommandModel:
    def __init__(self, command: str):
        self.command = command
        self.count = 0

    async def stream(self, request):
        self.count += 1
        if self.count > 1:
            yield ModelEvent(
                kind=ModelEventKind.COMPLETED,
                response=ModelResponse(id="final", text="Continued", output=[]),
            )
            return
        call = ToolCall(
            id="command",
            name="run_command",
            arguments={"command": self.command, "yield_seconds": 10},
        )
        yield ModelEvent(
            kind=ModelEventKind.COMPLETED,
            response=ModelResponse(
                id="first",
                calls=[call],
                output=[
                    {
                        "type": NativeItemType.FUNCTION_CALL,
                        "call_id": call.id,
                        "name": call.name,
                        "arguments": call.arguments.model_dump_json(),
                    }
                ],
            ),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("during_workspace_locks", [False, True])
async def test_cancel_during_command_directory_setup_is_terminal_without_launch(
    tmp_path, monkeypatch, during_workspace_locks
):
    store = SessionStore(tmp_path / "home")
    await store.open()
    session = await store.create_session(tmp_path)
    config = AppConfig()
    config.runtime.approval_mode = ApprovalMode.NEVER
    config.runtime.allow_commands = True
    service = ToolService(config, store)
    runtime = AgentRuntime(config, store, CommandModel("echo never-launched"), service)
    reached = asyncio.Event()
    release = asyncio.Event()
    to_thread = asyncio.to_thread

    loop = asyncio.get_running_loop()
    mkdir = Path.mkdir

    def observed_mkdir(path, *args, **kwargs):
        value = mkdir(path, *args, **kwargs)
        selected = (
            path.name == "workspace-locks"
            if during_workspace_locks
            else path.parent.name == "commands"
        )
        if selected:
            loop.call_soon_threadsafe(reached.set)
        return value

    async def gated_directory(function, *args, **kwargs):
        value = await to_thread(function, *args, **kwargs)
        if reached.is_set() and not release.is_set():
            await release.wait()
        return value

    async def emit(event):
        pass

    monkeypatch.setattr(Path, "mkdir", observed_mkdir)
    monkeypatch.setattr(asyncio, "to_thread", gated_directory)
    try:
        task = asyncio.create_task(runtime.run(session, "Run", emit))
        await asyncio.wait_for(reached.wait(), 10)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 10)
        records = await store.read(session)
        results = [
            ToolResultCommitted.model_validate(record.payload).result
            for record in records
            if record.type == JournalEventType.TOOL_RESULT_COMMITTED
        ]
        assert len(results) == 1
        assert results[0].status == ToolStatus.CANCELLED
        assert results[0].content.exit_code is None
        assert "before process launch" in results[0].content.notice
        assert not runtime._unresolved_unknown(records)
        assert not service.owned_commands(session)
        validate_pairs(await runtime.context.load(session, records))
    finally:
        release.set()
        await service.close()
        await store.close()


@pytest.mark.asyncio
async def test_cancel_owned_command_before_coroutine_start_is_known(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / "home")
    await store.open()
    session = await store.create_session(tmp_path)
    config = AppConfig()
    config.runtime.approval_mode = ApprovalMode.NEVER
    config.runtime.allow_commands = True
    service = ToolService(config, store)
    runtime = AgentRuntime(config, store, CommandModel("echo never-launched"), service)
    create_task = asyncio.create_task

    def cancel_command_before_start(coroutine, **kwargs):
        task = create_task(coroutine, **kwargs)
        if coroutine.cr_code.co_name == "run_owned":
            task.cancel()
        return task

    async def emit(event):
        pass

    monkeypatch.setattr(asyncio, "create_task", cancel_command_before_start)
    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(runtime.run(session, "Run", emit), 10)
        records = await store.read(session)
        results = [
            ToolResultCommitted.model_validate(record.payload).result
            for record in records
            if record.type == JournalEventType.TOOL_RESULT_COMMITTED
        ]
        assert len(results) == 1
        assert results[0].status == ToolStatus.CANCELLED
        assert results[0].content.exit_code is None
        assert not results[0].content.side_effect_result_unknown
        assert not runtime._unresolved_unknown(records)
        assert not service.owned_commands(session)
        validate_pairs(await runtime.context.load(session, records))
    finally:
        await service.close()
        await store.close()


@pytest.mark.asyncio
async def test_repeated_cancel_confirms_tree_exit_and_preserves_output(tmp_path):
    ready = asyncio.Event()
    killing = asyncio.Event()
    marker = tmp_path / "child-effect"
    child = f"import time,pathlib;time.sleep(1);pathlib.Path({str(marker)!r}).write_text('late')"
    parent = f"import subprocess,sys,time;subprocess.Popen([sys.executable,'-c',{child!r}]);print('ready',flush=True);print('error-output',file=sys.stderr,flush=True);time.sleep(30)"

    class SlowTermination(ProcessRunner):
        async def terminate(self, process):
            killing.set()
            await asyncio.sleep(0.15)
            await super().terminate(process)

    async def output(channel, text):
        if "ready" in text:
            ready.set()

    task = asyncio.create_task(
        SlowTermination().run([sys.executable, "-c", parent], tmp_path, on_output=output)
    )
    await asyncio.wait_for(ready.wait(), 10)
    task.cancel()
    await asyncio.wait_for(killing.wait(), 10)
    for _ in range(3):
        task.cancel()
        await asyncio.sleep(0)
    result = await asyncio.wait_for(task, 10)
    assert result.cancelled
    assert not result.side_effect_result_unknown
    assert result.exit_code is not None
    assert "ready" in result.stdout
    assert "error-output" in result.stderr
    assert "effects" in result.notice
    await asyncio.sleep(1.1)
    assert not marker.exists()


@pytest.mark.asyncio
async def test_cancel_before_workspace_lock_never_launches_process(tmp_path):
    store = SessionStore(tmp_path / "home")
    await store.open()
    session = await store.create_session(tmp_path)
    config = AppConfig()
    config.runtime.approval_mode = ApprovalMode.NEVER
    service = ToolService(config, store)
    context = ToolContext(
        workspace=tmp_path, home=store.home, session_id=session, run_id="run", allow_commands=True
    )
    workspace_lock = WorkspaceLock(workspace=str(tmp_path.resolve()))
    service.locks.append(workspace_lock)
    lock = workspace_lock.lock
    await lock.acquire()

    async def emit(event):
        pass

    async def unexpected(*args, **kwargs):
        pytest.fail("Process must not launch while waiting for workspace lock")

    service.process.run = unexpected
    try:
        first = await service.execute(
            ToolCall(
                id="waiting",
                name=ToolName.RUN_COMMAND,
                arguments={"command": "unused", "yield_seconds": 0.1},
            ),
            context,
            emit,
        )
        assert first.content.running
        result = await service.execute(
            ToolCall(
                id="stop",
                name=ToolName.STOP_COMMAND,
                arguments={"process_handle": first.content.process_handle},
            ),
            context,
            emit,
        )
        assert result.status == ToolStatus.CANCELLED
        assert result.content.exit_code is None
        assert not result.content.side_effect_result_unknown
    finally:
        lock.release()
        await service.close()
        await store.close()


@pytest.mark.asyncio
async def test_cancelled_local_command_journal_pairs_and_resumes_without_replay(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    marker = workspace / "effect"
    script = workspace / "command.py"
    script.write_text(
        "import pathlib,time,sys\npathlib.Path('effect').write_text('once')\nprint('ready',flush=True)\nprint('partial-error',file=sys.stderr,flush=True)\ntime.sleep(30)\n",
        encoding="utf-8",
    )
    command = (
        f"& '{sys.executable}' '{script}'" if os.name == "nt" else f"'{sys.executable}' '{script}'"
    )
    config = AppConfig()
    config.runtime.approval_mode = ApprovalMode.NEVER
    config.runtime.allow_commands = True
    ready = asyncio.Event()

    async def emit(event: RuntimeEvent):
        if event.kind == RuntimeEventKind.TOOL_OUTPUT_CHUNK and "ready" in event.data.text:
            ready.set()

    store = SessionStore(home)
    await store.open()
    tools = ToolService(config, store)
    model = CommandModel(command)
    runtime = AgentRuntime(config, store, model, tools)
    session = await store.create_session(workspace)
    result_durable = asyncio.Event()
    release_projection = asyncio.Event()
    project = store._project

    async def gated_projection(scanned, *, allow_stale=False):
        if (
            scanned
            and scanned[-1][0].type == JournalEventType.TOOL_RESULT_COMMITTED
            and not release_projection.is_set()
        ):
            result_durable.set()
            await release_projection.wait()
        await project(scanned, allow_stale=allow_stale)

    monkeypatch.setattr(store, "_project", gated_projection)
    try:
        task = asyncio.create_task(runtime.run(session, "Run once", emit))
        await asyncio.wait_for(ready.wait(), 15)
        task.cancel()
        await asyncio.wait_for(result_durable.wait(), 15)
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0)
        assert not task.done()
        release_projection.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 15)
        records = await store.read(session)
        results = [
            ToolResultCommitted.model_validate(record.payload).result
            for record in records
            if record.type == JournalEventType.TOOL_RESULT_COMMITTED
        ]
        assert len(results) == 1
        assert results[0].status == ToolStatus.CANCELLED
        assert results[0].content.exit_code is not None
        assert "ready" in results[0].content.stdout
        assert "partial-error" in results[0].content.stderr
        assert results[0].content.stdout_artifact_id
        validate_pairs(await runtime.context.load(session, records))
        assert not runtime._unresolved_unknown(records)
        assert not tools.owned_commands(session)
        assert (await runtime.prepare_continuation(session)).action == ContinuationAction.PREPARED
        assert records[-1].payload.status == RunStatus.CANCELLED
        marker.write_text("external", encoding="utf-8")
        await tools.close()
        await store.close()
        await ProjectionMaintenance(home).rebuild()
        await store.open()
        fresh_tools = ToolService(config, store)
        fresh_model = CommandModel(command)
        fresh_model.count = 1
        try:
            resumed = await AgentRuntime(config, store, fresh_model, fresh_tools).run(
                session, "Continue", emit
            )
            assert resumed.stop_reason == StopReason.COMPLETED
            assert marker.read_text() == "external"
            assert fresh_model.count == 2
        finally:
            await fresh_tools.close()
    finally:
        release_projection.set()
        await tools.close()
        await store.close()


@pytest.mark.asyncio
async def test_cancel_after_process_exit_before_result_commit_preserves_success(
    tmp_path, monkeypatch
):
    store = SessionStore(tmp_path / "home")
    await store.open()
    config = AppConfig()
    config.runtime.approval_mode = ApprovalMode.NEVER
    config.runtime.allow_commands = True
    tools = ToolService(config, store)
    runtime = AgentRuntime(config, store, CommandModel("echo completed"), tools)
    session = await store.create_session(tmp_path)
    result_ready = asyncio.Event()
    commit = runtime._result
    waiting = True

    async def pause_first_result(session_id, run_id, result):
        nonlocal waiting
        if waiting:
            waiting = False
            result_ready.set()
            await asyncio.Event().wait()
        await commit(session_id, run_id, result)

    async def emit(event):
        pass

    monkeypatch.setattr(runtime, "_result", pause_first_result)
    try:
        task = asyncio.create_task(runtime.run(session, "Run", emit))
        await asyncio.wait_for(result_ready.wait(), 15)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 15)
        records = await store.read(session)
        results = [
            ToolResultCommitted.model_validate(record.payload).result
            for record in records
            if record.type == JournalEventType.TOOL_RESULT_COMMITTED
        ]
        assert len(results) == 1
        assert results[0].status == ToolStatus.SUCCEEDED
        assert results[0].content.exit_code == 0
        assert "completed" in results[0].content.stdout
        assert not runtime._unresolved_unknown(records)
        assert not tools.owned_commands(session)
        validate_pairs(await runtime.context.load(session, records))
    finally:
        await tools.close()
        await store.close()
