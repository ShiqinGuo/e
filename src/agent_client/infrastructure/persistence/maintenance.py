import asyncio
import hashlib
import os
import shutil
from pathlib import Path
from typing import cast
from uuid import uuid4

from sqlalchemy import delete, func, select

from agent_client.domain.enums import ErrorCode, JournalEventType
from agent_client.domain.errors import AgentError
from agent_client.domain.events import JournalRecord
from agent_client.domain.persistence import (
    JournalProjectionSnapshot,
    MaintenanceKind,
    MaintenanceManifest,
    MaintenanceState,
    PersistenceConfig,
    PreservedProjectionFile,
    ProjectionRebuildResult,
    SessionDeletionResult,
)
from agent_client.domain.runtime import (
    BackgroundProcessSettled,
    CompactionCommitted,
    ToolResultCommitted,
)
from agent_client.domain.skills import SkillLoadResult, SkillResourceResult
from agent_client.domain.tools import ToolOutputProjection
from agent_client.domain.workspace import FileWriteResult, ProcessResult
from agent_client.infrastructure.persistence.models import (
    CheckpointRow,
    EventRow,
    InputRow,
    RunRow,
    SessionRow,
    ToolRow,
)
from agent_client.infrastructure.persistence.ownership import offline_maintenance
from agent_client.infrastructure.persistence.store import (
    IDENTIFIER,
    SessionStore,
    atomic_write,
    canonical,
    finish_durable,
    io_path,
    sync_directory,
)

PROJECTION_FILES = ("state.sqlite-wal", "state.sqlite-shm", "state.sqlite")


def file_digest(path: Path, *, chunk_bytes: int = 1048576) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_bytes):
            digest.update(chunk)
    return digest.hexdigest()


def write_manifest(path: Path, manifest: MaintenanceManifest) -> None:
    atomic_write(path, canonical(manifest))


def read_manifest(path: Path) -> MaintenanceManifest:
    try:
        manifest = MaintenanceManifest.model_validate_json(path.read_bytes())
        if path.name == "manifest.json" and manifest.operation_id != path.parent.name:
            raise ValueError("Maintenance operation identity does not match its directory")
        return manifest
    except ValueError as error:
        raise AgentError(
            ErrorCode.UNKNOWN_OUTCOME,
            "Maintenance manifest is damaged; preserve the directory for inspection",
        ) from error


def promote_projection(home: Path, operation: Path, manifest: MaintenanceManifest) -> None:
    if manifest.projection_sha256 is None:
        raise AgentError(ErrorCode.UNKNOWN_OUTCOME, "Verified projection checksum is unavailable")
    staged = operation / "staging.sqlite"
    target = home / "state.sqlite"
    retired = operation / "retired"
    retired.mkdir(parents=True, exist_ok=True)
    installed = (
        not staged.exists()
        and target.exists()
        and file_digest(target) == manifest.projection_sha256
    )
    if not installed and (not staged.exists() or file_digest(staged) != manifest.projection_sha256):
        raise AgentError(
            ErrorCode.UNKNOWN_OUTCOME, "Verified staging projection is missing or damaged"
        )
    for name in PROJECTION_FILES:
        source = home / name
        if not source.exists() or name == "state.sqlite" and installed:
            continue
        preserved = next((item for item in manifest.preserved_files if item.name == name), None)
        if preserved is None or file_digest(source) != preserved.sha256:
            raise AgentError(
                ErrorCode.PROJECTION_CONFLICT, "Projection files changed during maintenance"
            )
        os.replace(source, retired / name)
        sync_directory(home)
        sync_directory(retired)
    if not installed:
        os.replace(staged, target)
        sync_directory(home)
    manifest.state = MaintenanceState.COMPLETED
    write_manifest(operation / "manifest.json", manifest)


def recover_projection_switches(home: Path) -> None:
    for path in sorted((home / "maintenance").glob("*/manifest.json")):
        manifest = read_manifest(path)
        if manifest.kind != MaintenanceKind.REBUILD:
            continue
        match manifest.state:
            case MaintenanceState.READY:
                promote_projection(home, path.parent, manifest)
            case MaintenanceState.PREPARED | MaintenanceState.BACKED_UP:
                raise AgentError(
                    ErrorCode.UNKNOWN_OUTCOME,
                    "Projection rebuild is unfinished; run the explicit rebuild command",
                )


def tombstones(home: Path) -> list[MaintenanceManifest]:
    result: list[MaintenanceManifest] = []
    for path in sorted((home / "tombstones").glob("*.json")):
        manifest = read_manifest(path)
        if manifest.kind != MaintenanceKind.DELETE_SESSION or manifest.session_id != path.stem:
            raise AgentError(
                ErrorCode.UNKNOWN_OUTCOME, "Deletion tombstone identity does not match its path"
            )
        result.append(manifest)
    return result


def quarantine_session(home: Path, manifest: MaintenanceManifest) -> None:
    if manifest.session_id is None:
        raise AgentError(ErrorCode.INVALID_SESSION, "Deletion session identifier is unavailable")
    source = io_path(home / "sessions" / manifest.session_id)
    destination = io_path(home / "trash" / manifest.operation_id / "session")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.exists():
        if destination.exists():
            destination = destination.parent / f"restored-{uuid4().hex}"
        os.replace(source, destination)
        sync_directory(source.parent)
        sync_directory(destination.parent)


async def remove_projection(store: SessionStore, session_id: str) -> None:
    async with store.sessions.begin() as database:
        for model in (CheckpointRow, InputRow, ToolRow, RunRow, EventRow):
            await database.execute(delete(model).where(model.session_id == session_id))
        await database.execute(delete(SessionRow).where(SessionRow.id == session_id))


async def reconcile_deletions(store: SessionStore) -> None:
    for manifest in await asyncio.to_thread(tombstones, store.home):
        await asyncio.to_thread(quarantine_session, store.home, manifest)
        if manifest.session_id is None:
            raise AgentError(
                ErrorCode.INVALID_SESSION, "Deletion session identifier is unavailable"
            )
        await remove_projection(store, manifest.session_id)
        manifest.state = MaintenanceState.COMPLETED
        await asyncio.to_thread(
            write_manifest, store.home / "tombstones" / f"{manifest.session_id}.json", manifest
        )


class ProjectionMaintenance:
    def __init__(self, home: Path, *, config: PersistenceConfig | None = None):
        self.config = config if config is not None else PersistenceConfig()
        self.home = home.expanduser().resolve()

    def _journals(self) -> list[Path]:
        root = io_path(self.home / "sessions")
        if not root.exists():
            return []
        journals: list[Path] = []
        for directory in sorted(root.iterdir()):
            if not directory.is_dir():
                raise AgentError(
                    ErrorCode.JOURNAL_CORRUPT, "Session storage contains an unexpected file"
                )
            if not IDENTIFIER.fullmatch(directory.name):
                raise AgentError(
                    ErrorCode.INVALID_SESSION, "Session storage contains an invalid identifier"
                )
            journal = directory / "rollout.jsonl"
            if not journal.is_file():
                raise AgentError(
                    ErrorCode.JOURNAL_CORRUPT,
                    "A session directory has no journal; preserve it for inspection",
                )
            journals.append(journal)
        return journals

    def _retry_unfinished_builds(self) -> None:
        for path in sorted((self.home / "maintenance").glob("*/manifest.json")):
            manifest = read_manifest(path)
            if manifest.kind == MaintenanceKind.REBUILD and manifest.state in {
                MaintenanceState.PREPARED,
                MaintenanceState.BACKED_UP,
            }:
                manifest.state = MaintenanceState.FAILED
                manifest.error = "Superseded by an explicit rebuild retry"
                write_manifest(path, manifest)

    def _backup_projection(self, operation: Path, manifest: MaintenanceManifest) -> None:
        directory = operation / "previous"
        directory.mkdir(parents=True, exist_ok=True)
        for name in PROJECTION_FILES:
            source = self.home / name
            if not source.exists():
                continue
            digest = file_digest(source)
            with source.open("rb") as reader, (directory / name).open("xb") as writer:
                shutil.copyfileobj(reader, writer, length=self.config.copy_chunk_bytes)
                writer.flush()
                os.fsync(writer.fileno())
            if file_digest(directory / name) != digest or file_digest(source) != digest:
                raise AgentError(
                    ErrorCode.PROJECTION_CONFLICT, "Projection changed while preserving its files"
                )
            manifest.preserved_files.append(
                PreservedProjectionFile(name=name, sha256=digest, size=source.stat().st_size)
            )
        sync_directory(directory)
        manifest.state = MaintenanceState.BACKED_UP
        write_manifest(operation / "manifest.json", manifest)

    def _validate_references(
        self, scanner: SessionStore, session_id: str, records: list[JournalRecord]
    ) -> None:
        references: set[str] = set()
        for record in records:
            match record.type:
                case JournalEventType.COMPACTION_COMMITTED:
                    references.add(cast(CompactionCommitted, record.payload).artifact_id)
                case JournalEventType.TOOL_RESULT_COMMITTED:
                    result = cast(ToolResultCommitted, record.payload).result
                    if result.artifact_id is not None:
                        references.add(result.artifact_id)
                    match result.content:
                        case FileWriteResult() as content:
                            owned = (content.before_artifact_id, content.after_artifact_id)
                        case ProcessResult() | ToolOutputProjection() as content:
                            owned = (content.stdout_artifact_id, content.stderr_artifact_id)
                        case SkillLoadResult() | SkillResourceResult() as content:
                            owned = (content.snapshot_artifact_id,)
                        case _:
                            owned = ()
                    references.update(value for value in owned if value is not None)
                case JournalEventType.BACKGROUND_PROCESS_SETTLED:
                    process = cast(BackgroundProcessSettled, record.payload).result
                    references.update(
                        value
                        for value in (process.stdout_artifact_id, process.stderr_artifact_id)
                        if value is not None
                    )
        directory = scanner._directory(session_id) / "artifacts"
        for identity in references:
            path = directory / identity
            if not path.is_file():
                raise AgentError(
                    ErrorCode.ARTIFACT_MISSING, "A journal-referenced artifact is unavailable"
                )
            if file_digest(path) != identity:
                raise AgentError(
                    ErrorCode.ARTIFACT_CORRUPT, "A journal-referenced artifact failed its checksum"
                )

    async def _verify_projection(
        self, builder: SessionStore, snapshots: list[JournalProjectionSnapshot]
    ) -> None:
        async with builder.sessions() as database:
            count = await database.scalar(select(func.count()).select_from(SessionRow))
            if count != len(snapshots):
                raise AgentError(
                    ErrorCode.PROJECTION_CONFLICT,
                    "Rebuilt session count does not match the journals",
                )
            for snapshot in snapshots:
                row = await database.get(SessionRow, snapshot.session_id)
                event_count = await database.scalar(
                    select(func.count())
                    .select_from(EventRow)
                    .where(EventRow.session_id == snapshot.session_id)
                )
                if (
                    row is None
                    or row.seq != snapshot.seq
                    or row.record_hash != snapshot.record_hash
                    or row.byte_offset != snapshot.byte_offset
                    or event_count != snapshot.seq
                ):
                    raise AgentError(
                        ErrorCode.PROJECTION_CONFLICT,
                        "Rebuilt projection cursor does not match its journal",
                    )

    async def rebuild(self) -> ProjectionRebuildResult:
        return await finish_durable(self._rebuild())

    async def _rebuild(self) -> ProjectionRebuildResult:
        async with offline_maintenance(self.home):
            await asyncio.to_thread(self._retry_unfinished_builds)
            await asyncio.to_thread(recover_projection_switches, self.home)
            for manifest in await asyncio.to_thread(tombstones, self.home):
                await asyncio.to_thread(quarantine_session, self.home, manifest)
            operation_id = uuid4().hex
            operation = io_path(self.home / "maintenance" / operation_id)
            await asyncio.to_thread(operation.mkdir, parents=True)
            manifest = MaintenanceManifest(operation_id=operation_id, kind=MaintenanceKind.REBUILD)
            scanner = SessionStore(self.home)
            snapshots: list[JournalProjectionSnapshot] = []
            scans: list[list[tuple[JournalRecord, int, int]]] = []
            try:
                await asyncio.to_thread(write_manifest, operation / "manifest.json", manifest)
                await asyncio.to_thread(self._backup_projection, operation, manifest)
                journals = await asyncio.to_thread(self._journals)
                for journal in journals:
                    scanned = await asyncio.to_thread(
                        scanner._scan, journal.parent.name, strict_tail=True
                    )
                    await asyncio.to_thread(
                        self._validate_references,
                        scanner,
                        journal.parent.name,
                        [record for record, _, _ in scanned],
                    )
                    last, offset, length = scanned[-1]
                    snapshots.append(
                        JournalProjectionSnapshot(
                            session_id=last.session_id,
                            seq=last.seq,
                            record_hash=last.record_hash,
                            byte_offset=offset + length,
                        )
                    )
                    scans.append(scanned)
                manifest.session_count = len(snapshots)
                manifest.record_count = sum(snapshot.seq for snapshot in snapshots)
                await asyncio.to_thread(write_manifest, operation / "manifest.json", manifest)
                builder = SessionStore(self.home, database_path=operation / "staging.sqlite")
                try:
                    await builder._initialize_projection()
                    for scanned in scans:
                        await builder._project(scanned)
                    await self._verify_projection(builder, snapshots)
                finally:
                    await builder.close()
                if await asyncio.to_thread(
                    lambda: (
                        (operation / "staging.sqlite-wal").exists()
                        and (operation / "staging.sqlite-wal").stat().st_size > 0
                    )
                ):
                    raise AgentError(
                        ErrorCode.UNKNOWN_OUTCOME,
                        "Staging projection WAL did not checkpoint on close",
                    )
                manifest.projection_sha256 = await asyncio.to_thread(
                    file_digest, operation / "staging.sqlite"
                )
                manifest.state = MaintenanceState.READY
                await asyncio.to_thread(write_manifest, operation / "manifest.json", manifest)
                await asyncio.to_thread(promote_projection, self.home, operation, manifest)
                return ProjectionRebuildResult(
                    operation_id=operation_id,
                    state=manifest.state,
                    backup_directory=operation / "previous",
                    session_count=manifest.session_count,
                    record_count=manifest.record_count,
                )
            except Exception as error:
                if manifest.state not in {MaintenanceState.READY, MaintenanceState.COMPLETED}:
                    manifest.state = MaintenanceState.FAILED
                    manifest.error = type(error).__name__
                    await asyncio.to_thread(write_manifest, operation / "manifest.json", manifest)
                raise
            finally:
                await scanner.close()

    async def delete_session(self, session_id: str) -> SessionDeletionResult:
        if not IDENTIFIER.fullmatch(session_id):
            raise AgentError(ErrorCode.INVALID_SESSION, "Invalid session identifier")
        return await finish_durable(self._delete_session(session_id))

    async def _delete_session(self, session_id: str) -> SessionDeletionResult:
        async with offline_maintenance(self.home):
            await asyncio.to_thread(recover_projection_switches, self.home)
            existing = self.home / "tombstones" / f"{session_id}.json"
            if await asyncio.to_thread(existing.exists):
                manifest = await asyncio.to_thread(read_manifest, existing)
                if (
                    manifest.kind != MaintenanceKind.DELETE_SESSION
                    or manifest.session_id != session_id
                ):
                    raise AgentError(
                        ErrorCode.UNKNOWN_OUTCOME,
                        "Deletion tombstone identity does not match its path",
                    )
            else:
                scanner = SessionStore(self.home)
                try:
                    scanned = await asyncio.to_thread(scanner._scan, session_id, strict_tail=True)
                    if not scanned:
                        raise AgentError(ErrorCode.SESSION_MISSING, "Session journal was not found")
                finally:
                    await scanner.close()
                manifest = MaintenanceManifest(
                    operation_id=uuid4().hex,
                    kind=MaintenanceKind.DELETE_SESSION,
                    session_id=session_id,
                )
                await asyncio.to_thread(write_manifest, existing, manifest)
            await asyncio.to_thread(quarantine_session, self.home, manifest)
            projection = SessionStore(self.home)
            try:
                await projection._initialize_projection()
                await reconcile_deletions(projection)
            finally:
                await projection.close()
            return SessionDeletionResult(
                operation_id=manifest.operation_id,
                state=MaintenanceState.COMPLETED,
                session_id=session_id,
                trash_directory=io_path(self.home / "trash" / manifest.operation_id),
            )
