import asyncio
import hashlib
import json
import os
import re
import shutil
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from uuid import uuid4

from alembic import command
from alembic.config import Config
from filelock import FileLock, Timeout
from pydantic import BaseModel
from sqlalchemy import event, select
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agent_client.domain.enums import (
    ErrorCode,
    JournalEventType,
    RunStatus,
    ToolExecutionState,
)
from agent_client.domain.errors import AgentError
from agent_client.domain.events import JournalPayload, JournalRecord, journal_payload_type
from agent_client.domain.models import SessionInfo
from agent_client.domain.persistence import (
    BackupManifest,
    CheckpointReference,
    JournalFormatVersion,
    JournalPayloadVersion,
    JournalProjectionSnapshot,
    JournalWireRecord,
    PersistenceConfig,
    SessionCreatedPayload,
)
from agent_client.domain.runtime import (
    CompactionCommitted,
    RunFinished,
    ToolResultCommitted,
    ToolStateChange,
    UserMessage,
)
from agent_client.domain.workspace import PlatformKind
from agent_client.infrastructure.persistence.models import (
    CheckpointRow,
    EventRow,
    InputRow,
    RunRow,
    SessionRow,
    ToolRow,
)
from agent_client.infrastructure.persistence.ownership import acquire_client_lease, maintenance_gate

IDENTIFIER = re.compile(r"^[a-zA-Z0-9_-]{1,100}$")


async def finish_durable[T](operation: Awaitable[T]) -> T:
    task = asyncio.ensure_future(operation)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


def io_path(path: Path) -> Path:
    if os.name != PlatformKind.WINDOWS:
        return path
    raw = str(path.resolve())
    match raw:
        case value if value.startswith("\\\\?\\"):
            return Path(value)
        case value if value.startswith("\\\\"):
            return Path("\\\\?\\UNC\\" + value[2:])
        case value:
            return Path("\\\\?\\" + value)


def canonical(value: BaseModel, *, exclude: set[str] | None = None) -> bytes:
    return json.dumps(
        value.model_dump(mode="json", exclude=exclude),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def record_digest(record: JournalRecord) -> str:
    return hashlib.sha256(canonical(record, exclude={"record_hash"})).hexdigest()


def sync_directory(path: Path) -> None:
    if os.name != PlatformKind.WINDOWS:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass(slots=True)
class SessionAppendLock:
    session_id: str
    lock: asyncio.Lock


class SessionStore:
    def __init__(
        self,
        home: Path,
        *,
        database_path: Path | None = None,
        config: PersistenceConfig | None = None,
    ):
        self.config = config if config is not None else PersistenceConfig()
        self.home = home.expanduser().resolve()
        self.database_path = database_path or self.home / "state.sqlite"
        self.engine = create_async_engine(
            URL.create("sqlite+aiosqlite", database=str(self.database_path))
        )
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self._append_locks: list[SessionAppendLock] = []
        self._project_lock = asyncio.Lock()
        self._maintenance_lock = asyncio.Lock()
        self._opened = False
        self._lifecycle_lock = asyncio.Lock()
        self._client_lease: FileLock | None = None

        @event.listens_for(self.engine.sync_engine, "connect")
        def configure_sqlite(connection, _record):
            cursor = connection.cursor()
            for pragma in (
                "PRAGMA foreign_keys=ON",
                "PRAGMA journal_mode=WAL",
                "PRAGMA synchronous=FULL",
                f"PRAGMA busy_timeout={self.config.busy_timeout_milliseconds}",
            ):
                cursor.execute(pragma)
            cursor.close()

    async def open(self) -> "SessionStore":
        return await finish_durable(self._open())

    async def _open(self) -> "SessionStore":
        async with self._lifecycle_lock:
            if self._opened:
                return self
            async with maintenance_gate(self.home):
                from agent_client.infrastructure.persistence.maintenance import (
                    reconcile_deletions,
                    recover_projection_switches,
                )

                await asyncio.to_thread(recover_projection_switches, self.home)
                self._client_lease = await acquire_client_lease(self.home)
                try:
                    await self._initialize_projection()
                    await reconcile_deletions(self)
                    self._opened = True
                except BaseException:
                    await self.engine.dispose()
                    await asyncio.to_thread(self._client_lease.release)
                    self._client_lease = None
                    raise
        return self

    async def _initialize_projection(self) -> None:
        await asyncio.to_thread(self.database_path.parent.mkdir, parents=True, exist_ok=True)
        config = Config()
        config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
        migration_lock = FileLock(str(self.home / "migration.lock"), thread_local=False)
        await asyncio.to_thread(migration_lock.acquire, timeout=self.config.writer_timeout_seconds)
        try:
            async with self.engine.begin() as connection:

                def upgrade(sync_connection):
                    config.attributes["connection"] = sync_connection
                    command.upgrade(config, "head")

                await connection.run_sync(upgrade)
        finally:
            await asyncio.to_thread(migration_lock.release)

    async def close(self) -> None:
        async with self._maintenance_lock, self._lifecycle_lock:
            await self.engine.dispose()
            if self._client_lease is not None:
                await asyncio.to_thread(self._client_lease.release)
                self._client_lease = None
            self._opened = False

    async def _maintained[T](self, operation: Awaitable[T]) -> T:
        async def owned() -> T:
            await self.open()
            async with self._maintenance_lock:
                lock = FileLock(str(self.home / "maintenance.lock"), thread_local=False)
                await asyncio.to_thread(lock.acquire, timeout=self.config.writer_timeout_seconds)
                try:
                    return await operation
                finally:
                    await asyncio.to_thread(lock.release)

        try:
            return await finish_durable(owned())
        finally:
            if asyncio.iscoroutine(operation):
                operation.close()

    def _directory(self, session_id: str) -> Path:
        if not IDENTIFIER.fullmatch(session_id):
            raise AgentError(ErrorCode.INVALID_SESSION, "Invalid session identifier")
        directory = self.home / "sessions" / session_id
        return io_path(directory)

    async def _assert_live_session(self, session_id: str) -> None:
        if not IDENTIFIER.fullmatch(session_id):
            raise AgentError(ErrorCode.INVALID_SESSION, "Invalid session identifier")
        if await asyncio.to_thread((self.home / "tombstones" / f"{session_id}.json").exists):
            raise AgentError(ErrorCode.SESSION_MISSING, "Session has been deleted")

    def _journal(self, session_id: str) -> Path:
        return self._directory(session_id) / "rollout.jsonl"

    @asynccontextmanager
    async def session_lock(self, session_id: str) -> AsyncIterator[None]:
        await self.open()
        await self._assert_live_session(session_id)
        lock_path = self._directory(session_id) / "execution.lock"
        await asyncio.to_thread(lock_path.parent.mkdir, parents=True, exist_ok=True)
        lock = FileLock(str(lock_path), thread_local=False)
        try:
            await asyncio.to_thread(lock.acquire, timeout=0)
        except Timeout as error:
            raise AgentError(
                ErrorCode.SESSION_BUSY, "Session is already running in another client"
            ) from error
        try:
            yield
        finally:
            await asyncio.to_thread(lock.release)

    def _scan(
        self, session_id: str, *, repair_tail: bool = False, strict_tail: bool = False
    ) -> list[tuple[JournalRecord, int, int]]:
        path = self._journal(session_id)
        if not path.exists():
            return []
        data = path.read_bytes()
        records: list[tuple[JournalRecord, int, int]] = []
        offset = 0
        identities: set[str] = set()
        for line in data.splitlines(keepends=True):
            if not line.endswith(b"\n"):
                if strict_tail:
                    raise AgentError(
                        ErrorCode.JOURNAL_CORRUPT, "Journal contains an incomplete final record"
                    )
                if repair_tail:
                    atomic_write(path.with_name(f"damaged-tail-{uuid4().hex}.bin"), line)
                    with path.open("r+b") as stream:
                        stream.truncate(offset)
                        stream.flush()
                        os.fsync(stream.fileno())
                break
            try:
                wire = JournalWireRecord.model_validate_json(line)
                wire_digest = hashlib.sha256(canonical(wire, exclude={"record_hash"})).hexdigest()
                if (
                    wire.log_format_version != JournalFormatVersion.CURRENT
                    or wire.payload_version
                    not in {JournalPayloadVersion.LEGACY, JournalPayloadVersion.TYPED}
                ):
                    raise AgentError(
                        ErrorCode.JOURNAL_VERSION, "Unsupported journal record version"
                    )
                record = JournalRecord.model_validate_json(line)
            except (ValueError, KeyError) as error:
                raise AgentError(
                    ErrorCode.JOURNAL_CORRUPT, f"Invalid journal record at offset {offset}"
                ) from error
            if (
                record.session_id != session_id
                or record.seq != len(records) + 1
                or record.event_id in identities
                or record.record_hash != wire_digest
            ):
                raise AgentError(
                    ErrorCode.JOURNAL_CORRUPT, f"Journal integrity failure at offset {offset}"
                )
            identities.add(record.event_id)
            if not records and record.type != JournalEventType.SESSION_CREATED:
                raise AgentError(
                    ErrorCode.JOURNAL_CORRUPT, "Journal does not start with SessionCreated"
                )
            records.append((record, offset, len(line)))
            offset += len(line)
        if not records:
            raise AgentError(
                ErrorCode.JOURNAL_CORRUPT, "Journal has no complete SessionCreated record"
            )
        return records

    async def read(self, session_id: str) -> list[JournalRecord]:
        await self._assert_live_session(session_id)
        scanned = await asyncio.to_thread(self._scan, session_id)
        return [record for record, _, _ in scanned]

    async def append(
        self,
        session_id: str,
        type: JournalEventType,
        payload: JournalPayload,
        run_id: str | None = None,
        event_id: str | None = None,
    ) -> JournalRecord:
        if not isinstance(type, JournalEventType):
            raise AgentError(ErrorCode.JOURNAL_TYPE, f"Unsupported journal event type: {type}")
        if not isinstance(payload, journal_payload_type(type)):
            raise TypeError(f"{type} requires {journal_payload_type(type).__name__}")
        return await self._maintained(self._append(session_id, type, payload, run_id, event_id))

    async def _append(
        self,
        session_id: str,
        type: JournalEventType,
        payload: JournalPayload,
        run_id: str | None,
        event_id: str | None,
    ) -> JournalRecord:
        await self.open()
        await self._assert_live_session(session_id)
        identity = event_id or uuid4().hex
        entry = next(
            (entry for entry in self._append_locks if entry.session_id == session_id), None
        )
        if entry is None:
            entry = SessionAppendLock(session_id=session_id, lock=asyncio.Lock())
            self._append_locks.append(entry)
        local_lock = entry.lock
        async with local_lock:
            directory = self._directory(session_id)
            await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True)
            writer_lock = FileLock(str(directory / "writer.lock"), thread_local=False)
            await asyncio.to_thread(writer_lock.acquire, timeout=self.config.writer_timeout_seconds)
            try:
                scanned = await asyncio.to_thread(self._scan, session_id, repair_tail=True)
                await self._project(scanned)
                existing = next((r for r, _, _ in scanned if r.event_id == identity), None)
                if existing is not None:
                    if (
                        existing.type != type
                        or existing.payload != payload
                        or existing.run_id != run_id
                    ):
                        raise AgentError(
                            ErrorCode.EVENT_CONFLICT,
                            "Event identity was reused with different content",
                        )
                    return existing
                if not scanned and type != JournalEventType.SESSION_CREATED:
                    raise AgentError(
                        ErrorCode.SESSION_MISSING, "Session must be created before appending events"
                    )
                if scanned and type == JournalEventType.SESSION_CREATED:
                    raise AgentError(ErrorCode.SESSION_EXISTS, "Session already exists")
                record = JournalRecord(
                    session_id=session_id,
                    seq=len(scanned) + 1,
                    event_id=identity,
                    run_id=run_id,
                    type=type,
                    payload=payload,
                )
                record.record_hash = record_digest(record)
                encoded = canonical(record) + b"\n"
                offset = sum(length for _, _, length in scanned)
                await asyncio.to_thread(self._write_line, self._journal(session_id), encoded)
                scanned.append((record, offset, len(encoded)))
                await self._project(scanned)
                return record
            finally:
                await asyncio.to_thread(writer_lock.release)

    @staticmethod
    def _write_line(path: Path, encoded: bytes) -> None:
        created = not path.exists()
        with path.open("ab") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        if created:
            sync_directory(path.parent)

    async def _project(
        self, scanned: list[tuple[JournalRecord, int, int]], *, allow_stale: bool = False
    ) -> None:
        if not scanned:
            return
        async with self._project_lock, self.sessions.begin() as database:
            session_id = scanned[0][0].session_id
            row = await database.get(SessionRow, session_id)
            if row and row.seq > len(scanned):
                if not allow_stale:
                    raise AgentError(
                        ErrorCode.PROJECTION_CONFLICT, "Database cursor leads the journal"
                    )
                current = await asyncio.to_thread(self._scan, session_id)
                if (
                    len(current) < row.seq
                    or current[row.seq - 1][0].record_hash != row.record_hash
                    or current[row.seq - 1][1] + current[row.seq - 1][2] != row.byte_offset
                ):
                    raise AgentError(
                        ErrorCode.PROJECTION_CONFLICT,
                        "Database cursor does not match the current journal",
                    )
                last, offset, length = scanned[-1]
                indexed = await database.scalar(
                    select(EventRow).where(
                        EventRow.session_id == session_id, EventRow.seq == last.seq
                    )
                )
                if (
                    indexed is None
                    or indexed.record_hash != last.record_hash
                    or indexed.byte_offset != offset
                    or indexed.byte_length != length
                ):
                    raise AgentError(
                        ErrorCode.PROJECTION_CONFLICT,
                        "Stale journal snapshot does not match indexed history",
                    )
                return
            if (
                row
                and row.seq
                and (
                    row.record_hash != scanned[row.seq - 1][0].record_hash
                    or row.byte_offset != scanned[row.seq - 1][1] + scanned[row.seq - 1][2]
                )
            ):
                raise AgentError(
                    ErrorCode.PROJECTION_CONFLICT, "Database cursor does not match the journal"
                )
            for record, offset, length in scanned:
                if row and record.seq <= row.seq:
                    continue
                if row is None:
                    if record.type != JournalEventType.SESSION_CREATED:
                        raise AgentError(
                            ErrorCode.JOURNAL_CORRUPT, "Journal does not start with SessionCreated"
                        )
                    created = cast(SessionCreatedPayload, record.payload)
                    row = SessionRow(
                        id=session_id,
                        workspace=created.workspace,
                        title=created.title,
                        status=RunStatus.IDLE,
                        seq=0,
                        record_hash="",
                        byte_offset=0,
                        context_epoch=0,
                    )
                    database.add(row)
                    await database.flush()
                database.add(
                    EventRow(
                        event_id=record.event_id,
                        session_id=session_id,
                        seq=record.seq,
                        type=record.type,
                        run_id=record.run_id,
                        byte_offset=offset,
                        byte_length=length,
                        record_hash=record.record_hash,
                    )
                )
                match record.type:
                    case JournalEventType.USER_MESSAGE:
                        message = cast(UserMessage, record.payload)
                        if message.command_id:
                            prior = await database.get(InputRow, (message.command_id, session_id))
                            if prior is None:
                                database.add(
                                    InputRow(
                                        command_id=message.command_id,
                                        session_id=session_id,
                                        run_id=record.run_id,
                                        seq=record.seq,
                                    )
                                )
                    case JournalEventType.RUN_STARTED if record.run_id:
                        database.add(
                            RunRow(
                                id=record.run_id, session_id=session_id, status=RunStatus.RUNNING
                            )
                        )
                        row.status = RunStatus.RUNNING
                    case JournalEventType.RUN_FINISHED if record.run_id:
                        finished = cast(RunFinished, record.payload)
                        run = await database.get(RunRow, (record.run_id, session_id))
                        if run:
                            run.status = finished.status
                            run.stop_reason = finished.stop_reason
                        row.status = finished.status
                    case JournalEventType.TOOL_CALL_STATE:
                        changed = cast(ToolStateChange, record.payload)
                        tool = await database.get(ToolRow, (changed.call_id, session_id))
                        if tool is None:
                            tool = ToolRow(
                                id=changed.call_id,
                                session_id=session_id,
                                run_id=record.run_id,
                                status=changed.state,
                                name=changed.call.name if changed.call else "",
                            )
                            database.add(tool)
                        else:
                            tool.status = changed.state
                    case JournalEventType.TOOL_RESULT_COMMITTED:
                        committed = cast(ToolResultCommitted, record.payload)
                        result = committed.result
                        tool = await database.get(ToolRow, (result.call_id, session_id))
                        state = ToolExecutionState(result.status.value.upper())
                        if tool is None:
                            tool = ToolRow(
                                id=result.call_id,
                                session_id=session_id,
                                run_id=record.run_id,
                                status=state,
                                name="",
                            )
                            database.add(tool)
                        else:
                            tool.status = state
                    case JournalEventType.COMPACTION_COMMITTED:
                        checkpoint = cast(CompactionCommitted, record.payload)
                        reference = CheckpointReference(artifact_id=checkpoint.artifact_id)
                        database.add(
                            CheckpointRow(
                                event_id=record.event_id,
                                session_id=session_id,
                                epoch=checkpoint.epoch,
                                source_seq=checkpoint.source_seq,
                                reference=reference,
                            )
                        )
                        row.context_epoch = checkpoint.epoch
                row.seq = record.seq
                row.record_hash = record.record_hash
                row.byte_offset = offset + length
                await database.flush()

    async def create_session(self, workspace: Path, title: str = "New session") -> str:
        workspace = workspace.resolve()
        if not workspace.is_dir():
            raise AgentError(ErrorCode.WORKSPACE_MISSING, "Workspace directory does not exist")
        identity = uuid4().hex
        await self.append(
            identity,
            JournalEventType.SESSION_CREATED,
            SessionCreatedPayload(workspace=str(workspace), title=title),
        )
        return identity

    async def recover(self, session_id: str) -> list[JournalRecord]:
        return await self._maintained(self._recover(session_id))

    async def _recover(self, session_id: str) -> list[JournalRecord]:
        await self.open()
        await self._assert_live_session(session_id)
        path = self._directory(session_id)
        if not self._journal(session_id).exists():
            raise AgentError(ErrorCode.SESSION_MISSING, "Session journal was not found")
        writer_lock = FileLock(str(path / "writer.lock"), thread_local=False)
        await asyncio.to_thread(writer_lock.acquire, timeout=self.config.writer_timeout_seconds)
        try:
            scanned = await asyncio.to_thread(self._scan, session_id, repair_tail=True)
            await self._project(scanned)
            return [record for record, _, _ in scanned]
        finally:
            await asyncio.to_thread(writer_lock.release)

    async def get_session(self, session_id: str) -> SessionInfo:
        await self.open()
        await self._assert_live_session(session_id)
        scanned = await asyncio.to_thread(self._scan, session_id)
        if not scanned:
            raise AgentError(ErrorCode.SESSION_MISSING, "Session journal was not found")
        await self._project(scanned, allow_stale=True)
        async with self.sessions() as database:
            row = await database.get(SessionRow, session_id)
            if row is None:
                raise AgentError(ErrorCode.SESSION_MISSING, "Session does not exist")
            return SessionInfo(
                id=row.id,
                workspace=row.workspace,
                title=row.title,
                status=row.status,
                seq=row.seq,
            )

    async def list_sessions(self) -> list[SessionInfo]:
        await self.open()
        root = self.home / "sessions"
        journals = await asyncio.to_thread(lambda: list(root.glob("*/rollout.jsonl")))
        for journal in journals:
            scanned = await asyncio.to_thread(self._scan, journal.parent.name)
            await self._project(scanned, allow_stale=True)
        async with self.sessions() as database:
            rows = (await database.scalars(select(SessionRow).order_by(SessionRow.id.desc()))).all()
            for row in rows:
                if not await asyncio.to_thread(self._journal(row.id).is_file):
                    raise AgentError(
                        ErrorCode.JOURNAL_CORRUPT, "A projected session has no journal"
                    )
            return [
                SessionInfo(
                    id=row.id,
                    workspace=row.workspace,
                    title=row.title,
                    status=row.status,
                    seq=row.seq,
                )
                for row in rows
            ]

    async def put_artifact(self, session_id: str, content: str) -> str:
        return await self._maintained(self._put_artifact(session_id, content))

    async def _put_artifact(self, session_id: str, content: str) -> str:
        await self._assert_live_session(session_id)
        encoded = content.encode("utf-8")
        identity = hashlib.sha256(encoded).hexdigest()
        path = self._directory(session_id) / "artifacts" / identity
        await asyncio.to_thread(atomic_write, path, encoded)
        return identity

    async def read_artifact(self, session_id: str, artifact_id: str) -> str:
        await self._assert_live_session(session_id)
        if not re.fullmatch(r"[a-f0-9]{64}", artifact_id):
            raise AgentError(ErrorCode.INVALID_ARTIFACT, "Invalid artifact identifier")
        path = self._directory(session_id) / "artifacts" / artifact_id
        try:
            content = await asyncio.to_thread(path.read_bytes)
        except FileNotFoundError as error:
            raise AgentError(
                ErrorCode.ARTIFACT_MISSING, "Referenced output is unavailable"
            ) from error
        if hashlib.sha256(content).hexdigest() != artifact_id:
            raise AgentError(ErrorCode.ARTIFACT_CORRUPT, "Output integrity check failed")
        return content.decode("utf-8")

    async def put_artifact_file(self, session_id: str, source: Path) -> str:
        return await self._maintained(self._put_artifact_file(session_id, source))

    async def _put_artifact_file(self, session_id: str, source: Path) -> str:
        await self._assert_live_session(session_id)
        directory = self._directory(session_id) / "artifacts"

        def copy() -> str:
            directory.mkdir(parents=True, exist_ok=True)
            temporary = directory / f".{uuid4().hex}.tmp"
            digest = hashlib.sha256()
            try:
                with source.open("rb") as reader, temporary.open("xb") as writer:
                    while chunk := reader.read(self.config.copy_chunk_bytes):
                        digest.update(chunk)
                        writer.write(chunk)
                    writer.flush()
                    os.fsync(writer.fileno())
                identity = digest.hexdigest()
                os.replace(temporary, directory / identity)
                sync_directory(directory)
                return identity
            finally:
                temporary.unlink(missing_ok=True)

        return await asyncio.to_thread(copy)

    async def backup(self, destination: Path) -> Path:
        return await self._maintained(self._backup(destination))

    async def _backup(self, destination: Path) -> Path:
        if destination.exists():
            raise AgentError(ErrorCode.BACKUP_EXISTS, "Backup destination already exists")
        destination = destination.resolve()
        if destination.is_relative_to(self.home):
            raise AgentError(
                ErrorCode.BACKUP_PATH, "Backup must be outside the agent data directory"
            )
        destination = io_path(destination)
        infos = await self.list_sessions()
        locks: list[FileLock] = []
        boundaries: list[JournalProjectionSnapshot] = []
        try:
            for info in sorted(infos, key=lambda item: item.id):
                lock = FileLock(
                    str(self._directory(info.id) / "execution.lock"), thread_local=False
                )
                await asyncio.to_thread(lock.acquire, timeout=0)
                locks.append(lock)
            for info in sorted(infos, key=lambda item: item.id):
                scanned = await asyncio.to_thread(self._scan, info.id)
                record, offset, length = scanned[-1]
                boundaries.append(
                    JournalProjectionSnapshot(
                        session_id=info.id,
                        seq=record.seq,
                        record_hash=record.record_hash,
                        byte_offset=offset + length,
                    )
                )
            await asyncio.to_thread(destination.mkdir, parents=True)
            source = io_path(self.home / "sessions")
            if source.exists():
                await asyncio.to_thread(
                    shutil.copytree,
                    source,
                    destination / "sessions",
                    ignore=shutil.ignore_patterns("*.lock", "*.tmp"),
                )
            for boundary in boundaries:
                await asyncio.to_thread(
                    self._truncate_backup_journal,
                    destination / "sessions" / boundary.session_id / "rollout.jsonl",
                    boundary,
                )
            config = self.home / "config.toml"
            if config.exists():
                await asyncio.to_thread(shutil.copy2, config, destination / "config.toml")
            await asyncio.to_thread(
                atomic_write,
                destination / "manifest.json",
                canonical(BackupManifest(sessions=infos, journal_boundaries=boundaries)),
            )
            return destination
        except Timeout as error:
            raise AgentError(
                ErrorCode.SESSION_BUSY, "Stop running sessions before backing up"
            ) from error
        finally:
            for lock in reversed(locks):
                await asyncio.to_thread(lock.release)

    @staticmethod
    def _truncate_backup_journal(path: Path, boundary: JournalProjectionSnapshot) -> None:
        if path.stat().st_size < boundary.byte_offset:
            raise AgentError(
                ErrorCode.JOURNAL_CORRUPT, "Backup journal is shorter than its verified boundary"
            )
        with path.open("r+b") as stream:
            stream.truncate(boundary.byte_offset)
            stream.flush()
            os.fsync(stream.fileno())
