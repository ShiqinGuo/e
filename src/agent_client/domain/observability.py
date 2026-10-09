from datetime import datetime
from enum import StrEnum

from agent_client.domain.enums import (
    CompactionReason,
    ErrorCode,
    RunStatus,
    RuntimeEventKind,
    StopReason,
    TokenMeasurement,
    ToolStatus,
)
from agent_client.domain.models import Contract, Effect


class LogLevel(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class DiagnosticDetails(Contract):
    call_id: str | None = None
    name: str | None = None
    effect: Effect | None = None
    status: RunStatus | ToolStatus | None = None
    stop_reason: StopReason | ErrorCode | None = None
    epoch: int | None = None
    reason: CompactionReason | None = None
    step_id: str | None = None
    prefix_revision: str | None = None
    context_epoch: int | None = None
    before_tokens: int | None = None
    after_tokens: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_tokens: int | None = None
    cached_input_tokens: int | None = None
    cache_write_tokens: int | None = None
    duration_ms: float | None = None
    token_measurement: TokenMeasurement | None = None
    attempt: int | None = None
    error_code: ErrorCode | None = None
    context_hash: str | None = None
    estimated_input_tokens: int | None = None
    ttft_seconds: float | None = None
    duration_seconds: float | None = None
    seq: int | None = None
    applied_seq: int | None = None
    is_error: bool | None = None


class DiagnosticRecord(Contract):
    time: datetime
    level: LogLevel
    event: RuntimeEventKind
    session_id: str
    run_id: str
    trace_id: str | None
    details: DiagnosticDetails
