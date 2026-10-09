from collections.abc import Awaitable, Callable, Mapping
from typing import TypedDict, cast

from pydantic import Field, ValidationInfo, field_validator

from agent_client.domain.base import Contract
from agent_client.domain.context_usage import ContextUsage
from agent_client.domain.enums import JournalEventType, RuntimeEventKind, ToolStatus
from agent_client.domain.mcp import McpConnectionStatus, McpNamedStatus, McpStatus, McpToolEntry
from agent_client.domain.models import ReasoningBlock, ToolResult
from agent_client.domain.persistence import (
    JournalFormatVersion,
    JournalPayloadVersion,
    ObservationPayload,
    SessionCreatedPayload,
)
from agent_client.domain.protocol import NativeFunctionOutput, ProtocolObject
from agent_client.domain.runtime import (
    ApprovalDecision,
    BackgroundProcessSettled,
    CompactionCommitted,
    CompactionStarted,
    EpochChanged,
    ErrorOccurred,
    ModelCommitted,
    ModelCompleted,
    ModelFailure,
    ModelIncomplete,
    ModelRequestMetadata,
    PendingInput,
    RunFinished,
    RunStarted,
    TextDelta,
    ToolFinished,
    ToolResultCommitted,
    ToolStateChange,
    UnknownResolution,
    UserMessage,
)
from agent_client.domain.tools import (
    McpToolEntries,
    ToolContent,
    ToolDispatchEvent,
    ToolErrorContent,
    ToolOutputEvent,
    ToolOutputRange,
)

type JournalPayload = (
    ApprovalDecision
    | BackgroundProcessSettled
    | CompactionCommitted
    | CompactionStarted
    | ModelCommitted
    | ModelFailure
    | ModelIncomplete
    | ModelRequestMetadata
    | ObservationPayload
    | PendingInput
    | RunFinished
    | RunStarted
    | SessionCreatedPayload
    | ToolResultCommitted
    | ToolStateChange
    | UnknownResolution
    | UserMessage
)

type RuntimePayload = (
    UserMessage
    | ContextUsage
    | EpochChanged
    | ErrorOccurred
    | ModelCompleted
    | ModelRequestMetadata
    | ReasoningBlock
    | RunFinished
    | TextDelta
    | ToolDispatchEvent
    | ToolFinished
    | ToolOutputEvent
)


def journal_payload_type(kind: JournalEventType) -> type[Contract]:
    match kind:
        case JournalEventType.SESSION_CREATED:
            return SessionCreatedPayload
        case JournalEventType.PENDING_INPUT:
            return PendingInput
        case JournalEventType.USER_MESSAGE:
            return UserMessage
        case JournalEventType.RUN_STARTED:
            return RunStarted
        case JournalEventType.RUN_FINISHED:
            return RunFinished
        case JournalEventType.TOOL_CALL_STATE:
            return ToolStateChange
        case JournalEventType.TOOL_RESULT_COMMITTED:
            return ToolResultCommitted
        case JournalEventType.APPROVAL_DECIDED:
            return ApprovalDecision
        case JournalEventType.COMPACTION_STARTED:
            return CompactionStarted
        case JournalEventType.COMPACTION_COMMITTED:
            return CompactionCommitted
        case JournalEventType.MODEL_REQUEST_STARTED:
            return ModelRequestMetadata
        case JournalEventType.MODEL_REQUEST_FAILED:
            return ModelFailure
        case JournalEventType.MODEL_RESPONSE_COMMITTED:
            return ModelCommitted
        case JournalEventType.MODEL_RESPONSE_INCOMPLETE:
            return ModelIncomplete
        case JournalEventType.UNKNOWN_OUTCOME_RESOLVED:
            return UnknownResolution
        case JournalEventType.BACKGROUND_PROCESS_SETTLED:
            return BackgroundProcessSettled
        case JournalEventType.OBSERVATION:
            return ObservationPayload


def runtime_payload_type(kind: RuntimeEventKind) -> type[Contract] | None:
    match kind:
        case RuntimeEventKind.INPUT_STEERED:
            return UserMessage
        case RuntimeEventKind.CONTEXT_USAGE:
            return ContextUsage
        case RuntimeEventKind.TEXT_DELTA:
            return TextDelta
        case RuntimeEventKind.REASONING_DELTA:
            return ReasoningBlock
        case RuntimeEventKind.TOOL_DISPATCHING:
            return ToolDispatchEvent
        case RuntimeEventKind.TOOL_OUTPUT_CHUNK:
            return ToolOutputEvent
        case RuntimeEventKind.TOOL_OUTPUT:
            return ToolFinished
        case RuntimeEventKind.TOOL_FINISHED:
            return ToolFinished
        case RuntimeEventKind.MODEL_REQUEST_STARTED:
            return ModelRequestMetadata
        case RuntimeEventKind.MODEL_COMPLETED:
            return ModelCompleted
        case RuntimeEventKind.COMPACTION_FINISHED:
            return EpochChanged
        case RuntimeEventKind.RUN_FINISHED:
            return RunFinished
        case RuntimeEventKind.ERROR:
            return ErrorOccurred
        case RuntimeEventKind.COMPACTION_STARTED:
            return None


class JournalHeader(TypedDict):
    type: JournalEventType
    payload_version: JournalPayloadVersion


class RuntimeHeader(TypedDict):
    kind: RuntimeEventKind


class LegacyStatusWire(TypedDict):
    status: McpConnectionStatus
    protocol_version: str | None
    error: str | None


class LegacyMcpToolEntries(Contract):
    servers: ProtocolObject
    tools: list[McpToolEntry]
    notice: str


class LegacyToolResult(Contract):
    call_id: str = Field(min_length=1)
    content: ToolContent | LegacyMcpToolEntries | str
    is_error: bool = False
    status: ToolStatus = ToolStatus.SUCCEEDED
    artifact_id: str | None = None


class LegacyToolResultCommitted(Contract):
    result: LegacyToolResult
    item: NativeFunctionOutput


class JournalRecord(Contract):
    log_format_version: JournalFormatVersion = JournalFormatVersion.CURRENT
    session_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,100}$")
    seq: int = Field(ge=1)
    event_id: str = Field(min_length=1)
    run_id: str | None = None
    type: JournalEventType
    payload_version: JournalPayloadVersion = JournalPayloadVersion.TYPED
    payload: JournalPayload
    record_hash: str = ""

    @field_validator("payload", mode="before")
    @classmethod
    def validate_payload(cls, value: object, info: ValidationInfo) -> Contract:
        header = cast(JournalHeader, info.data)
        if (
            header["payload_version"] == JournalPayloadVersion.LEGACY
            and header["type"] == JournalEventType.TOOL_RESULT_COMMITTED
        ):
            legacy = LegacyToolResultCommitted.model_validate(value)
            content = legacy.result.content
            if isinstance(content, LegacyMcpToolEntries):
                wire = content.servers.wire_value()
                if not isinstance(wire, Mapping):
                    raise ValueError("Legacy MCP server directory must be an object")
                statuses = cast(Mapping[str, LegacyStatusWire], wire)
                named: list[McpNamedStatus] = []
                for name, status in statuses.items():
                    parsed = McpStatus.model_validate(status)
                    named.append(
                        McpNamedStatus(
                            name=name,
                            status=parsed.status,
                            protocol_version=parsed.protocol_version,
                            error=parsed.error,
                        )
                    )
                content = McpToolEntries(servers=named, tools=content.tools, notice=content.notice)
            if isinstance(content, str):
                content = (
                    ToolErrorContent(error=content)
                    if legacy.result.is_error
                    else ToolOutputRange(content=content, total_characters=len(content))
                )
            return ToolResultCommitted(
                result=ToolResult(
                    call_id=legacy.result.call_id,
                    content=content,
                    is_error=legacy.result.is_error,
                    status=legacy.result.status,
                    artifact_id=legacy.result.artifact_id,
                ),
                item=legacy.item,
            )
        return journal_payload_type(header["type"]).model_validate(value)


class RuntimeEvent(Contract):
    kind: RuntimeEventKind
    session_id: str = ""
    run_id: str = ""
    data: RuntimePayload | None = None

    @field_validator("data", mode="before")
    @classmethod
    def validate_data(cls, value: object, info: ValidationInfo) -> Contract | None:
        header = cast(RuntimeHeader, info.data)
        expected = runtime_payload_type(header["kind"])
        if expected is None:
            if value is not None:
                raise ValueError("This runtime event carries no payload")
            return None
        return expected.model_validate(value)


type EventSink = Callable[[RuntimeEvent], Awaitable[None]]
