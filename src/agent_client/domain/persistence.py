from enum import IntEnum, StrEnum
from pathlib import Path

from pydantic import Field, JsonValue, model_validator

from agent_client.domain.base import Contract
from agent_client.domain.enums import JournalEventType
from agent_client.domain.models import SessionInfo


class BackupProjectionStrategy(StrEnum):
    REBUILD_FROM_JOURNALS = "rebuild_from_journals"


class SessionCreatedPayload(Contract):
    workspace: str = Field(min_length=1)
    title: str = Field(default="New session", min_length=1)


class ObservationPayload(Contract):
    value: int


class JournalProjectionSnapshot(Contract):
    session_id: str
    seq: int = Field(ge=1)
    record_hash: str
    byte_offset: int = Field(ge=1)


class BackupManifest(Contract):
    version: int = Field(default=1, ge=1, le=1)
    sessions: list[SessionInfo]
    journal_boundaries: list[JournalProjectionSnapshot]
    credentials_included: bool = False
    sqlite: BackupProjectionStrategy = BackupProjectionStrategy.REBUILD_FROM_JOURNALS


class CheckpointReference(Contract):
    artifact_id: str = Field(pattern=r"^[a-f0-9]{64}$")


class MaintenanceKind(StrEnum):
    REBUILD = "rebuild"
    DELETE_SESSION = "delete_session"


class MaintenanceState(StrEnum):
    PREPARED = "prepared"
    BACKED_UP = "backed_up"
    READY = "ready"
    COMPLETED = "completed"
    FAILED = "failed"


class PreservedProjectionFile(Contract):
    name: str = Field(pattern=r"^state\.sqlite(?:-wal|-shm)?$")
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size: int = Field(ge=0)


class MaintenanceManifest(Contract):
    version: int = Field(default=1, ge=1, le=1)
    operation_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    kind: MaintenanceKind
    state: MaintenanceState = MaintenanceState.PREPARED
    session_id: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_-]{1,100}$")
    preserved_files: list[PreservedProjectionFile] = Field(default_factory=list)
    session_count: int = Field(default=0, ge=0)
    record_count: int = Field(default=0, ge=0)
    projection_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    error: str | None = None

    @model_validator(mode="after")
    def validate_operation(self):
        if self.kind == MaintenanceKind.DELETE_SESSION and self.session_id is None:
            raise ValueError("Deletion maintenance requires a session identifier")
        if (
            self.kind == MaintenanceKind.REBUILD
            and self.state in {MaintenanceState.READY, MaintenanceState.COMPLETED}
            and self.projection_sha256 is None
        ):
            raise ValueError("Ready projection maintenance requires a verified checksum")
        return self


class ProjectionRebuildResult(Contract):
    operation_id: str
    state: MaintenanceState
    backup_directory: Path
    session_count: int = Field(ge=0)
    record_count: int = Field(ge=0)


class SessionDeletionResult(Contract):
    operation_id: str
    state: MaintenanceState
    session_id: str
    trash_directory: Path


class JournalFormatVersion(IntEnum):
    CURRENT = 1


class JournalPayloadVersion(IntEnum):
    LEGACY = 1
    TYPED = 2


class JournalWireRecord(Contract):
    log_format_version: int = 1
    session_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,100}$")
    seq: int = Field(ge=1)
    event_id: str = Field(min_length=1)
    run_id: str | None = None
    type: JournalEventType
    payload_version: int = 1
    payload: JsonValue
    record_hash: str = ""


class PersistenceConfig(Contract):
    writer_timeout_seconds: float = Field(default=10, gt=0)
    copy_chunk_bytes: int = Field(default=1048576, gt=0)
    busy_timeout_milliseconds: int = Field(default=5000, ge=0)
