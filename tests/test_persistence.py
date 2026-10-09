import asyncio
import hashlib
import json

import pytest
from sqlalchemy import select

from agent_client.application.prompts import user_item
from agent_client.domain.enums import JournalEventType, RunStatus, StopReason
from agent_client.domain.errors import AgentError
from agent_client.domain.events import LegacyToolResult, LegacyToolResultCommitted
from agent_client.domain.persistence import (
    JournalPayloadVersion,
    JournalWireRecord,
    ObservationPayload,
)
from agent_client.domain.protocol import NativeFunctionOutput
from agent_client.domain.runtime import (
    RunFinished,
    RunStarted,
    UserMessage,
)
from agent_client.domain.tools import ToolOutputRange
from agent_client.infrastructure.persistence.models import SessionRow
from agent_client.infrastructure.persistence.store import SessionStore, canonical


@pytest.fixture
async def store(tmp_path):
    value = await SessionStore(tmp_path / "data").open()
    yield value
    await value.close()


async def test_jsonl_survives_projection_failure_and_replays_once(store, tmp_path, monkeypatch):
    session_id = await store.create_session(tmp_path)
    original = store._project
    calls = 0

    async def fail_second(records):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated disk error")
        await original(records)

    monkeypatch.setattr(store, "_project", fail_second)
    with pytest.raises(OSError):
        await store.append(
            session_id,
            JournalEventType.USER_MESSAGE,
            UserMessage(command_id="one", item=user_item("one").item),
            event_id="one",
        )
    assert len(await store.read(session_id)) == 2
    monkeypatch.setattr(store, "_project", original)
    await store.recover(session_id)
    record = await store.append(
        session_id,
        JournalEventType.USER_MESSAGE,
        UserMessage(command_id="one", item=user_item("one").item),
        event_id="one",
    )
    assert record.seq == 2
    async with store.sessions() as database:
        assert (await database.get(SessionRow, session_id)).seq == 2
    assert len(await store.read(session_id)) == 2


async def test_rebuild_missing_database_from_journal(tmp_path):
    home = tmp_path / "data"
    first = await SessionStore(home).open()
    session_id = await first.create_session(tmp_path, "Retained title")
    await first.append(
        session_id,
        JournalEventType.RUN_STARTED,
        RunStarted(
            status=RunStatus.RUNNING,
            model="test-model",
            prefix_revision="test-prefix",
            instructions="test instructions",
            tools=[],
        ),
        run_id="run-one",
    )
    await first.append(
        session_id,
        JournalEventType.RUN_FINISHED,
        RunFinished(status=RunStatus.COMPLETED, stop_reason=StopReason.COMPLETED),
        run_id="run-one",
    )
    await first.close()
    for path in home.glob("state.sqlite*"):
        path.unlink()
    second = await SessionStore(home).open()
    try:
        rows = await second.list_sessions()
        assert [(row.id, row.title, row.status, row.seq) for row in rows] == [
            (session_id, "Retained title", RunStatus.COMPLETED, 3)
        ]
    finally:
        await second.close()


async def test_tail_repair_preserves_bad_bytes_but_middle_corruption_stops(store, tmp_path):
    session_id = await store.create_session(tmp_path)
    path = store._journal(session_id)
    with path.open("ab") as stream:
        stream.write(b'{"half":')
    records = await store.recover(session_id)
    assert len(records) == 1
    assert next(path.parent.glob("damaged-tail-*.bin")).read_bytes() == b'{"half":'
    data = json.loads(path.read_text())
    data["payload"]["title"] = "tampered"
    path.write_text(json.dumps(data) + "\n", encoding="utf-8")
    with pytest.raises(AgentError, match="integrity"):
        await store.recover(session_id)


async def test_session_lock_blocks_other_store_instance(store, tmp_path):
    session_id = await store.create_session(tmp_path)
    second = await SessionStore(store.home).open()
    try:
        async with store.session_lock(session_id):
            with pytest.raises(AgentError, match="already running"):
                async with second.session_lock(session_id):
                    pytest.fail("second owner entered")
    finally:
        await second.close()


async def test_concurrent_append_has_contiguous_sequence(store, tmp_path):
    session_id = await store.create_session(tmp_path)
    await asyncio.gather(
        *(
            store.append(session_id, JournalEventType.OBSERVATION, ObservationPayload(value=n))
            for n in range(12)
        )
    )
    records = await store.read(session_id)
    assert [r.seq for r in records] == list(range(1, 14))
    async with store.sessions() as database:
        row = await database.scalar(select(SessionRow).where(SessionRow.id == session_id))
        assert row.seq == 13


async def test_artifact_scoped_and_integrity_checked(store, tmp_path):
    first = await store.create_session(tmp_path)
    second = await store.create_session(tmp_path)
    artifact = await store.put_artifact(first, "code and evidence")
    assert await store.read_artifact(first, artifact) == "code and evidence"
    with pytest.raises(AgentError, match="unavailable"):
        await store.read_artifact(second, artifact)
    with pytest.raises(AgentError, match="Invalid artifact"):
        await store.read_artifact(first, "../../state.sqlite")


async def test_backup_contains_journal_and_outputs_but_no_credentials(store, tmp_path):
    session_id = await store.create_session(tmp_path)
    artifact = await store.put_artifact(session_id, "retained")
    (store.home / "credentials.secret").write_text("do-not-copy")
    destination = await store.backup(tmp_path / "backup")
    assert (destination / "sessions" / session_id / "rollout.jsonl").exists()
    assert (
        destination / "sessions" / session_id / "artifacts" / artifact
    ).read_text() == "retained"
    assert not (destination / "credentials.secret").exists()
    restored = await SessionStore(destination).open()
    try:
        assert (await restored.list_sessions())[0].id == session_id
    finally:
        await restored.close()


async def test_empty_journal_cannot_reuse_an_old_projection(store, tmp_path):
    identity = await store.create_session(tmp_path)
    store._journal(identity).write_bytes(b"")
    with pytest.raises(AgentError, match="no complete"):
        await store.recover(identity)
    with pytest.raises(AgentError, match="no complete"):
        await store.list_sessions()


@pytest.mark.parametrize("version", [JournalPayloadVersion.LEGACY, JournalPayloadVersion.TYPED])
async def test_legacy_string_result_decodes_only_at_versioned_journal_boundary(tmp_path, version):
    store = await SessionStore(tmp_path / "home").open()
    session = await store.create_session(tmp_path)
    journal = store._journal(session)
    old = LegacyToolResultCommitted(
        result=LegacyToolResult(call_id="legacy", content="retained evidence"),
        item=NativeFunctionOutput(call_id="legacy", output="retained evidence"),
    )
    wire = JournalWireRecord(
        session_id=session,
        seq=2,
        event_id="legacy-result",
        type=JournalEventType.TOOL_RESULT_COMMITTED,
        payload_version=version,
        payload=old.model_dump(mode="json"),
    )
    wire.record_hash = hashlib.sha256(canonical(wire, exclude={"record_hash"})).hexdigest()
    encoded = journal.read_bytes() + canonical(wire) + b"\n"
    journal.write_bytes(encoded)
    try:
        match version:
            case JournalPayloadVersion.LEGACY:
                records = store._scan(session, repair_tail=False)
                restored = records[-1][0].payload.result
                assert isinstance(restored.content, ToolOutputRange)
                assert restored.content.content == "retained evidence"
                assert records[-1][0].record_hash == wire.record_hash
            case JournalPayloadVersion.TYPED:
                with pytest.raises(AgentError):
                    store._scan(session, repair_tail=False)
        assert journal.read_bytes() == encoded
    finally:
        await store.close()
