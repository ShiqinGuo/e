import asyncio
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import func, select

from agent_client.application.prompts import user_item
from agent_client.domain.enums import (
    ContextStrategy,
    ErrorCode,
    JournalEventType,
    RunStatus,
    StopReason,
    ToolExecutionState,
)
from agent_client.domain.errors import AgentError
from agent_client.domain.mcp import McpToolResult
from agent_client.domain.models import ToolCall, ToolResult
from agent_client.domain.persistence import (
    BackupManifest,
    MaintenanceKind,
    MaintenanceManifest,
    MaintenanceState,
)
from agent_client.domain.protocol import NativeFunctionOutput, ProtocolObject
from agent_client.domain.runtime import (
    CompactionCommitted,
    CompactionReason,
    RunFinished,
    RunStarted,
    ToolResultCommitted,
    ToolStateChange,
    UserMessage,
)
from agent_client.domain.tools import McpCallArguments, ToolName, ToolOutputRange
from agent_client.infrastructure.persistence import maintenance
from agent_client.infrastructure.persistence.maintenance import ProjectionMaintenance, read_manifest
from agent_client.infrastructure.persistence.models import (
    CheckpointRow,
    EventRow,
    InputRow,
    RunRow,
    SessionRow,
    ToolRow,
)
from agent_client.infrastructure.persistence.store import SessionStore, io_path


@dataclass(slots=True)
class ProjectionCounts:
    sessions: int
    events: int
    runs: int
    tools: int
    inputs: int
    checkpoints: int


async def projection_counts(store: SessionStore) -> ProjectionCounts:
    async with store.sessions() as database:
        values = [
            await database.scalar(select(func.count()).select_from(model))
            for model in (SessionRow, EventRow, RunRow, ToolRow, InputRow, CheckpointRow)
        ]
        return ProjectionCounts(
            sessions=values[0],
            events=values[1],
            runs=values[2],
            tools=values[3],
            inputs=values[4],
            checkpoints=values[5],
        )


async def seeded_session(home: Path, workspace: Path) -> str:
    store = await SessionStore(home).open()
    try:
        identity = await store.create_session(workspace, "Retained projection")
        await store.append(
            identity,
            JournalEventType.USER_MESSAGE,
            UserMessage(item=user_item("Inspect").item, command_id="command"),
        )
        await store.append(
            identity,
            JournalEventType.RUN_STARTED,
            RunStarted(
                status=RunStatus.RUNNING,
                model="fixture",
                prefix_revision="stable",
                instructions="fixture rules",
                tools=[],
            ),
            run_id="run",
        )
        await store.append(
            identity,
            JournalEventType.TOOL_CALL_STATE,
            ToolStateChange(
                call_id="tool",
                state=ToolExecutionState.PROPOSED,
                call=ToolCall(id="tool", name=ToolName.READ_FILE, arguments={"path": "example.py"}),
            ),
            run_id="run",
        )
        await store.append(
            identity,
            JournalEventType.TOOL_RESULT_COMMITTED,
            ToolResultCommitted(
                result=ToolResult(
                    call_id="tool",
                    content=ToolOutputRange(content="retained", total_characters=len("retained")),
                ),
                item=NativeFunctionOutput(call_id="tool", output="retained"),
            ),
            run_id="run",
        )
        artifact = await store.put_artifact(identity, "checkpoint")
        await store.append(
            identity,
            JournalEventType.COMPACTION_COMMITTED,
            CompactionCommitted(
                epoch=1,
                source_seq=5,
                artifact_id=artifact,
                strategy=ContextStrategy.SUMMARY,
                before_tokens=100,
                after_tokens=20,
                reason=CompactionReason.MANUAL,
            ),
            run_id="run",
        )
        await store.append(
            identity,
            JournalEventType.RUN_FINISHED,
            RunFinished(status=RunStatus.COMPLETED, stop_reason=StopReason.COMPLETED),
            run_id="run",
        )
        return identity
    finally:
        await store.close()


async def test_corrupt_projection_rebuild_preserves_family_and_is_equivalent(tmp_path):
    home = io_path(tmp_path / "home")
    identity = await seeded_session(home, tmp_path)
    prior = await SessionStore(home).open()
    expected = await projection_counts(prior)
    sessions = await prior.list_sessions()
    await prior.close()
    journal = home / "sessions" / identity / "rollout.jsonl"
    facts = journal.read_bytes()
    artifacts = {path.name: path.read_bytes() for path in (journal.parent / "artifacts").iterdir()}
    old_files = {
        "state.sqlite": b"corrupted projection",
        "state.sqlite-wal": b"preserved old WAL",
        "state.sqlite-shm": b"preserved old SHM",
    }
    for name, content in old_files.items():
        (home / name).write_bytes(content)
    (home / "credentials.secret").write_bytes(b"private credential")
    result = await ProjectionMaintenance(home).rebuild()
    assert result.state == MaintenanceState.COMPLETED
    assert result.record_count == 7 and result.session_count == 1
    for name, content in old_files.items():
        assert (result.backup_directory / name).read_bytes() == content
    restored = await SessionStore(home).open()
    try:
        assert await projection_counts(restored) == expected
        assert await restored.list_sessions() == sessions
    finally:
        await restored.close()
    assert journal.read_bytes() == facts
    assert {
        path.name: path.read_bytes() for path in (journal.parent / "artifacts").iterdir()
    } == artifacts
    assert (home / "credentials.secret").read_bytes() == b"private credential"


async def test_invalid_journal_stops_rebuild_and_preserves_original_files(tmp_path):
    home = io_path(tmp_path / "home")
    identity = await seeded_session(home, tmp_path)
    journal = home / "sessions" / identity / "rollout.jsonl"
    journal.write_bytes(journal.read_bytes() + b'{"unfinished":')
    facts = journal.read_bytes()
    original = (home / "state.sqlite").read_bytes()
    with pytest.raises(AgentError, match="incomplete"):
        await ProjectionMaintenance(home).rebuild()
    assert journal.read_bytes() == facts
    assert (home / "state.sqlite").read_bytes() == original
    manifest_path = next((home / "maintenance").glob("*/manifest.json"))
    assert read_manifest(manifest_path).state == MaintenanceState.FAILED
    assert (manifest_path.parent / "previous" / "state.sqlite").read_bytes() == original


async def test_client_lease_in_other_process_refuses_offline_maintenance(tmp_path):
    home = io_path(tmp_path / "home")
    script = tmp_path / "client.py"
    script.write_text(
        f'from pathlib import Path\nimport asyncio\nfrom agent_client.infrastructure.persistence.store import SessionStore\nasync def main():\n    store=await SessionStore(Path({str(home)!r})).open()\n    print("ready",flush=True)\n    await asyncio.sleep(30)\nasyncio.run(main())\n',
        encoding="utf-8",
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable, str(script), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        assert (await asyncio.wait_for(process.stdout.readline(), 10)).strip() == b"ready"
        with pytest.raises(AgentError) as failure:
            await ProjectionMaintenance(home).rebuild()
        assert failure.value.code == ErrorCode.SESSION_BUSY
        assert not (home / "maintenance").exists()
    finally:
        process.terminate()
        await process.wait()
    assert (await ProjectionMaintenance(home).rebuild()).state == MaintenanceState.COMPLETED


async def test_deletion_crash_and_old_projection_do_not_resurrect_session(tmp_path, monkeypatch):
    home = io_path(tmp_path / "home")
    deleted = await seeded_session(home, tmp_path)
    store = await SessionStore(home).open()
    retained = await store.create_session(tmp_path, "Retained session")
    await store.close()
    old_database = (home / "state.sqlite").read_bytes()
    facts = (home / "sessions" / deleted / "rollout.jsonl").read_bytes()
    original_remove = maintenance.remove_projection

    async def fail_after_quarantine(store: SessionStore, session_id: str) -> None:
        raise OSError("crash after directory isolation")

    monkeypatch.setattr(maintenance, "remove_projection", fail_after_quarantine)
    with pytest.raises(OSError, match="isolation"):
        await ProjectionMaintenance(home).delete_session(deleted)
    manifest = read_manifest(home / "tombstones" / f"{deleted}.json")
    assert not (home / "sessions" / deleted).exists()
    trash = home / "trash" / manifest.operation_id / "session"
    assert (trash / "rollout.jsonl").read_bytes() == facts
    (home / "state.sqlite").write_bytes(old_database)
    monkeypatch.setattr(maintenance, "remove_projection", original_remove)
    reopened = await SessionStore(home).open()
    try:
        assert [session.id for session in await reopened.list_sessions()] == [retained]
        assert (await projection_counts(reopened)).events == 1
        with pytest.raises(AgentError, match="deleted"):
            await reopened.recover(deleted)
    finally:
        await reopened.close()
    shutil.copytree(trash, home / "sessions" / deleted)
    reopened = await SessionStore(home).open()
    try:
        assert [session.id for session in await reopened.list_sessions()] == [retained]
        assert not (home / "sessions" / deleted).exists()
    finally:
        await reopened.close()


async def test_verified_switch_resumes_after_partial_family_rename(tmp_path, monkeypatch):
    home = io_path(tmp_path / "home")
    identity = await seeded_session(home, tmp_path)
    original_replace = maintenance.os.replace
    triggered = False

    def fail_install(source, destination):
        nonlocal triggered
        if Path(source).name == "staging.sqlite" and not triggered:
            triggered = True
            raise OSError("crash before installing verified projection")
        return original_replace(source, destination)

    monkeypatch.setattr(maintenance.os, "replace", fail_install)
    with pytest.raises(OSError, match="installing"):
        await ProjectionMaintenance(home).rebuild()
    assert not (home / "state.sqlite").exists()
    path = next((home / "maintenance").glob("*/manifest.json"))
    assert read_manifest(path).state == MaintenanceState.READY
    monkeypatch.setattr(maintenance.os, "replace", original_replace)
    opened = await SessionStore(home).open()
    try:
        assert [session.id for session in await opened.list_sessions()] == [identity]
    finally:
        await opened.close()
    assert read_manifest(path).state == MaintenanceState.COMPLETED


async def test_unverified_maintenance_does_not_create_empty_projection(tmp_path):
    home = io_path(tmp_path / "home")
    home.mkdir()
    operation_id = "a" * 32
    directory = home / "maintenance" / operation_id
    directory.mkdir(parents=True)
    manifest = MaintenanceManifest(
        operation_id=operation_id, kind=MaintenanceKind.REBUILD, state=MaintenanceState.BACKED_UP
    )
    (directory / "manifest.json").write_text(manifest.model_dump_json(), encoding="utf-8")
    store = SessionStore(home)
    try:
        with pytest.raises(AgentError, match="unfinished"):
            await store.open()
        assert not (home / "state.sqlite").exists()
    finally:
        await store.close()


async def test_existing_directory_without_journal_is_not_silently_dropped(tmp_path):
    home = io_path(tmp_path / "home")
    identity = await seeded_session(home, tmp_path)
    journal = home / "sessions" / identity / "rollout.jsonl"
    retained = journal.with_name("retained-journal.jsonl")
    journal.rename(retained)
    facts = retained.read_bytes()
    projection = (home / "state.sqlite").read_bytes()
    with pytest.raises(AgentError, match="no journal"):
        await ProjectionMaintenance(home).rebuild()
    assert retained.read_bytes() == facts
    assert (home / "state.sqlite").read_bytes() == projection
    assert (retained.parent / "artifacts").is_dir()


async def test_stale_execution_lock_does_not_block_directory_quarantine(tmp_path):
    home = io_path(tmp_path / "home")
    identity = await seeded_session(home, tmp_path)
    directory = home / "sessions" / identity
    (directory / "execution.lock").write_bytes(b"")
    facts = (directory / "rollout.jsonl").read_bytes()
    result = await ProjectionMaintenance(home).delete_session(identity)
    assert result.state == MaintenanceState.COMPLETED
    assert not directory.exists()
    assert (result.trash_directory / "session" / "rollout.jsonl").read_bytes() == facts


async def test_active_execution_without_client_lease_is_still_rejected(tmp_path):
    home = io_path(tmp_path / "home")
    identity = await seeded_session(home, tmp_path)
    lock_path = home / "sessions" / identity / "execution.lock"
    script = tmp_path / "execution_owner.py"
    script.write_text(
        f'from filelock import FileLock\nimport time\nlock=FileLock({str(lock_path)!r},thread_local=False)\nlock.acquire(timeout=0)\nprint("ready",flush=True)\ntime.sleep(30)\n',
        encoding="utf-8",
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable, str(script), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        assert (await asyncio.wait_for(process.stdout.readline(), 10)).strip() == b"ready"
        with pytest.raises(AgentError) as failure:
            await ProjectionMaintenance(home).delete_session(identity)
        assert failure.value.code == ErrorCode.SESSION_BUSY
        assert (home / "sessions" / identity / "rollout.jsonl").is_file()
        assert not (home / "tombstones").exists()
    finally:
        process.terminate()
        await process.wait()


async def test_external_mcp_json_artifact_key_is_not_a_local_reference(tmp_path):
    home = io_path(tmp_path / "home")
    store = await SessionStore(home).open()
    try:
        identity = await store.create_session(tmp_path)
        await store.append(
            identity,
            JournalEventType.TOOL_CALL_STATE,
            ToolStateChange(
                call_id="remote",
                state=ToolExecutionState.PROPOSED,
                call=ToolCall(
                    id="remote",
                    name=ToolName.CALL_MCP_TOOL,
                    arguments=McpCallArguments(
                        tool_id="server/tool",
                        schema_hash="a" * 64,
                        arguments=ProtocolObject.model_validate("{}"),
                    ),
                ),
            ),
        )
        await store.append(
            identity,
            JournalEventType.TOOL_RESULT_COMMITTED,
            ToolResultCommitted(
                result=ToolResult(
                    call_id="remote",
                    content=McpToolResult(
                        content=[],
                        structured_content=ProtocolObject.model_validate(
                            '{"artifact_id":"external-object-identifier"}'
                        ),
                    ),
                ),
                item=NativeFunctionOutput(call_id="remote", output="remote evidence"),
            ),
        )
    finally:
        await store.close()
    result = await ProjectionMaintenance(home).rebuild()
    assert result.state == MaintenanceState.COMPLETED and result.record_count == 3


async def test_missing_declared_artifact_reference_stops_rebuild(tmp_path):
    home = io_path(tmp_path / "home")
    identity = await seeded_session(home, tmp_path)
    directory = home / "sessions" / identity
    artifact = next((directory / "artifacts").iterdir())
    artifact.rename(artifact.with_name("retained-original-artifact"))
    facts = (directory / "rollout.jsonl").read_bytes()
    original_projection = (home / "state.sqlite").read_bytes()
    with pytest.raises(AgentError) as failure:
        await ProjectionMaintenance(home).rebuild()
    assert failure.value.code == ErrorCode.ARTIFACT_MISSING
    assert (directory / "rollout.jsonl").read_bytes() == facts
    assert (home / "state.sqlite").read_bytes() == original_projection


async def test_process_killed_after_quarantine_resumes_without_resurrection(tmp_path):
    home = io_path(tmp_path / "home")
    identity = await seeded_session(home, tmp_path)
    script = tmp_path / "delete_crash.py"
    script.write_text(
        f'from pathlib import Path\nimport asyncio\nfrom agent_client.infrastructure.persistence import maintenance\nasync def pause(store,session_id):\n    print("isolated",flush=True)\n    await asyncio.sleep(30)\nmaintenance.remove_projection=pause\nasyncio.run(maintenance.ProjectionMaintenance(Path({str(home)!r})).delete_session({identity!r}))\n',
        encoding="utf-8",
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable, str(script), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        assert (await asyncio.wait_for(process.stdout.readline(), 10)).strip() == b"isolated"
        assert not (home / "sessions" / identity).exists()
    finally:
        process.kill()
        await process.wait()
    reopened = await SessionStore(home).open()
    try:
        assert await reopened.list_sessions() == []
        assert (await projection_counts(reopened)).events == 0
    finally:
        await reopened.close()
    manifest = read_manifest(home / "tombstones" / f"{identity}.json")
    assert manifest.state == MaintenanceState.COMPLETED
    assert (home / "trash" / manifest.operation_id / "session" / "rollout.jsonl").is_file()


async def test_process_killed_between_projection_renames_finishes_verified_switch(tmp_path):
    home = io_path(tmp_path / "home")
    identity = await seeded_session(home, tmp_path)
    script = tmp_path / "rebuild_crash.py"
    script.write_text(
        f'from pathlib import Path\nimport asyncio,time\nfrom agent_client.infrastructure.persistence import maintenance\noriginal=maintenance.os.replace\ndef pause(source,destination):\n    if Path(source).name=="staging.sqlite":\n        print("ready-to-install",flush=True)\n        time.sleep(30)\n    return original(source,destination)\nmaintenance.os.replace=pause\nasyncio.run(maintenance.ProjectionMaintenance(Path({str(home)!r})).rebuild())\n',
        encoding="utf-8",
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable, str(script), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        assert (
            await asyncio.wait_for(process.stdout.readline(), 10)
        ).strip() == b"ready-to-install"
        assert not (home / "state.sqlite").exists()
    finally:
        process.kill()
        await process.wait()
    reopened = await SessionStore(home).open()
    try:
        assert [session.id for session in await reopened.list_sessions()] == [identity]
    finally:
        await reopened.close()
    manifest_path = next((home / "maintenance").glob("*/manifest.json"))
    assert read_manifest(manifest_path).state == MaintenanceState.COMPLETED


async def test_backup_uses_verified_boundary_without_repairing_source_tail(tmp_path):
    home = io_path(tmp_path / "home")
    identity = await seeded_session(home, tmp_path)
    store = await SessionStore(home).open()
    journal = home / "sessions" / identity / "rollout.jsonl"
    complete = journal.read_bytes()
    journal.write_bytes(complete + b'{"half":')
    original = journal.read_bytes()
    try:
        expected = (await store.read(identity))[-1]
        destination = await store.backup(tmp_path / "backup")
        manifest = BackupManifest.model_validate_json((destination / "manifest.json").read_bytes())
        boundary = manifest.journal_boundaries[0]
        assert boundary.session_id == identity
        assert boundary.seq == expected.seq
        assert boundary.record_hash == expected.record_hash
        assert boundary.byte_offset == len(complete)
        assert (destination / "sessions" / identity / "rollout.jsonl").read_bytes() == complete
        assert journal.read_bytes() == original
        result = await ProjectionMaintenance(destination).rebuild()
        assert result.state == MaintenanceState.COMPLETED
        assert result.record_count == expected.seq
        assert journal.read_bytes() == original
        first_boundary = complete.index(b"\n") + 1
        damaged = original[:first_boundary] + b"{invalid}\n" + original[first_boundary:]
        journal.write_bytes(damaged)
        with pytest.raises(AgentError) as failure:
            await store.backup(tmp_path / "invalid-backup")
        assert failure.value.code == ErrorCode.JOURNAL_CORRUPT
        assert journal.read_bytes() == damaged
        assert not (tmp_path / "invalid-backup").exists()
    finally:
        await store.close()
