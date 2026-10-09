import asyncio
import threading

import pytest
from filelock import FileLock, Timeout

from agent_client.domain.enums import JournalEventType
from agent_client.domain.errors import AgentError
from agent_client.domain.runtime import PendingInput
from agent_client.infrastructure.persistence import store as store_module
from agent_client.infrastructure.persistence.store import SessionStore


@pytest.fixture
async def store(tmp_path):
    value = SessionStore(tmp_path / "home")
    await value.open()
    yield value
    await value.close()


async def test_unknown_fact_type_stops_recovery_instead_of_advancing_projection(store, tmp_path):
    session = await store.create_session(tmp_path)
    with pytest.raises(AgentError, match="Unsupported|Unknown|unknown|unsupported"):
        await store.append(
            session,
            "FutureSideEffectDispatched",
            PendingInput(command_id="future", prompt="queued"),
        )
        await store.recover(session)


async def test_backup_freezes_journal_appends_until_snapshot_finishes(store, tmp_path, monkeypatch):
    session = await store.create_session(tmp_path)
    loop = asyncio.get_running_loop()
    copying = asyncio.Event()
    release = threading.Event()
    original = store_module.shutil.copytree

    def paused_copy(*args, **kwargs):
        loop.call_soon_threadsafe(copying.set)
        if not release.wait(5):
            raise RuntimeError("Review test copy gate timed out")
        return original(*args, **kwargs)

    monkeypatch.setattr(store_module.shutil, "copytree", paused_copy)
    backup = asyncio.create_task(store.backup(tmp_path / "backup"))
    await copying.wait()
    append = asyncio.create_task(
        store.append(
            session,
            JournalEventType.PENDING_INPUT,
            PendingInput(command_id="during-backup", prompt="queued"),
        )
    )
    try:
        await asyncio.sleep(0.1)
        assert not append.done(), "Backup execution locks do not stop queued journal writes"
    finally:
        release.set()
        await backup
        await append


async def test_repeated_cancellation_does_not_release_writer_before_thread_finishes(
    store, tmp_path, monkeypatch
):
    session = await store.create_session(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    original = store._write_line

    def paused_write(path, encoded):
        entered.set()
        release.wait(5)
        try:
            original(path, encoded)
        finally:
            finished.set()

    monkeypatch.setattr(store, "_write_line", paused_write)
    append = asyncio.create_task(
        store.append(
            session,
            JournalEventType.PENDING_INPUT,
            PendingInput(command_id="cancelled", prompt="queued"),
        )
    )
    await asyncio.to_thread(entered.wait, 5)
    append.cancel()
    await asyncio.sleep(0)
    append.cancel()
    await asyncio.sleep(0.1)
    contender = FileLock(str(store._directory(session) / "writer.lock"), thread_local=False)
    try:
        with pytest.raises(Timeout):
            await asyncio.to_thread(contender.acquire, timeout=0)
    finally:
        if contender.is_locked:
            await asyncio.to_thread(contender.release)
        release.set()
        await asyncio.to_thread(finished.wait, 5)
        await asyncio.gather(append, return_exceptions=True)


async def test_session_listing_does_not_treat_concurrent_append_as_projection_corruption(
    store, tmp_path, monkeypatch
):
    session = await store.create_session(tmp_path)
    scanned = threading.Event()
    release = threading.Event()
    original = store._scan
    first = True

    def delayed_scan(*args, **kwargs):
        nonlocal first
        if first:
            first = False
            result = original(*args, **kwargs)
            scanned.set()
            release.wait(5)
            return result
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "_scan", delayed_scan)
    listing = asyncio.create_task(store.list_sessions())
    await asyncio.to_thread(scanned.wait, 5)
    try:
        await store.append(
            session,
            JournalEventType.PENDING_INPUT,
            PendingInput(command_id="race", prompt="queued"),
        )
    finally:
        release.set()
    infos = await listing
    assert next((info for info in infos if info.id == session)).seq == 2


async def test_cross_session_event_identity_does_not_poison_durable_journal(store, tmp_path):
    first = await store.create_session(tmp_path)
    second = await store.create_session(tmp_path)
    payload = PendingInput(command_id="shared", prompt="queued")
    await store.append(first, JournalEventType.PENDING_INPUT, payload, event_id="pending:shared")
    try:
        await store.append(
            second, JournalEventType.PENDING_INPUT, payload, event_id="pending:shared"
        )
    except AgentError:
        assert len(await store.read(second)) == 1
    await store.recover(first)
    await store.recover(second)
