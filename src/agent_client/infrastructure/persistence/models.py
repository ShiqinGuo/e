from enum import StrEnum
from typing import TypedDict

from sqlalchemy import JSON, Enum, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from agent_client.domain.enums import (
    ErrorCode,
    JournalEventType,
    RunStatus,
    StopReason,
    ToolExecutionState,
)
from agent_client.domain.persistence import CheckpointReference


class CheckpointReferenceData(TypedDict):
    artifact_id: str


class CheckpointType(TypeDecorator[CheckpointReference]):
    impl = JSON
    cache_ok = True

    def process_bind_param(
        self, value: CheckpointReference | None, dialect
    ) -> CheckpointReferenceData | None:
        match value:
            case None:
                return None
            case CheckpointReference():
                return CheckpointReferenceData(artifact_id=value.artifact_id)
            case _:
                raise TypeError("Checkpoint storage requires a CheckpointReference")

    def process_result_value(
        self, value: CheckpointReferenceData | None, dialect
    ) -> CheckpointReference | None:
        return CheckpointReference.model_validate(value) if value is not None else None


class StopReasonType(TypeDecorator[StopReason | ErrorCode]):
    impl = String
    cache_ok = True

    def process_bind_param(self, value: StopReason | ErrorCode | None, dialect) -> str | None:
        match value:
            case None:
                return None
            case StopReason() | ErrorCode():
                return value.value
            case _:
                raise TypeError("Run termination requires a StopReason or ErrorCode")

    def process_result_value(self, value: str | None, dialect) -> StopReason | ErrorCode | None:
        if value is None:
            return None
        try:
            return StopReason(value)
        except ValueError:
            return ErrorCode(value)


def stored_enum[T: StrEnum](enum_type: type[T]) -> Enum:
    return Enum(
        enum_type,
        values_callable=lambda items: [item.value for item in items],
        native_enum=False,
        validate_strings=True,
    )


class Base(DeclarativeBase):
    pass


class SessionRow(Base):
    __tablename__ = "sessions"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text, default="New session")
    status: Mapped[RunStatus] = mapped_column(stored_enum(RunStatus), default=RunStatus.IDLE)
    seq: Mapped[int] = mapped_column(Integer, default=0)
    record_hash: Mapped[str] = mapped_column(String, default="")
    byte_offset: Mapped[int] = mapped_column(Integer, default=0)
    context_epoch: Mapped[int] = mapped_column(Integer, default=0)


class EventRow(Base):
    __tablename__ = "event_index"
    __table_args__ = (UniqueConstraint("session_id", "seq"),)
    event_id: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), primary_key=True)
    seq: Mapped[int] = mapped_column(Integer)
    type: Mapped[JournalEventType] = mapped_column(stored_enum(JournalEventType))
    run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    byte_offset: Mapped[int] = mapped_column(Integer)
    byte_length: Mapped[int] = mapped_column(Integer)
    record_hash: Mapped[str] = mapped_column(String)


class RunRow(Base):
    __tablename__ = "runs"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), primary_key=True)
    status: Mapped[RunStatus] = mapped_column(stored_enum(RunStatus))
    stop_reason: Mapped[StopReason | ErrorCode | None] = mapped_column(
        StopReasonType(), nullable=True
    )


class ToolRow(Base):
    __tablename__ = "tool_calls"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), primary_key=True)
    run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[ToolExecutionState] = mapped_column(stored_enum(ToolExecutionState))
    name: Mapped[str] = mapped_column(String, default="")


class InputRow(Base):
    __tablename__ = "inputs"
    command_id: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), primary_key=True)
    run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    seq: Mapped[int] = mapped_column(Integer)


class CheckpointRow(Base):
    __tablename__ = "context_checkpoints"
    __table_args__ = (UniqueConstraint("session_id", "epoch"),)
    event_id: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), primary_key=True)
    epoch: Mapped[int] = mapped_column(Integer)
    source_seq: Mapped[int] = mapped_column(Integer)
    reference: Mapped[CheckpointReference] = mapped_column(CheckpointType())
