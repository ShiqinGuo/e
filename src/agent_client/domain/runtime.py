from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import Field, field_serializer, field_validator, model_validator

from agent_client.domain.enums import (
    AuthMode,
    CompactionReason,
    ContextStrategy,
    ErrorCode,
    MessageRole,
    ProviderKind,
    RunStatus,
    StopReason,
    TokenMeasurement,
    ToolExecutionState,
    ToolStatus,
)
from agent_client.domain.models import (
    ApprovalRequest,
    Contract,
    ModelResponse,
    ReasoningBlock,
    ToolCall,
    ToolResult,
    ToolSpec,
)
from agent_client.domain.protocol import (
    ContentType,
    NativeContent,
    NativeFunctionCall,
    NativeFunctionOutput,
    NativeItem,
    NativeMessage,
    NativeReasoning,
)
from agent_client.domain.workspace import ProcessResult


class PendingInput(Contract):
    command_id: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    continuation_of: str | None = Field(default=None, min_length=1)
    target_run_id: str | None = Field(default=None, min_length=1)


class InputDisposition(StrEnum):
    STEERED = "steered"
    QUEUED = "queued"


class InputSubmission(Contract):
    command_id: str = Field(min_length=1)
    disposition: InputDisposition
    run_id: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def validate_disposition(self):
        if (self.disposition == InputDisposition.STEERED) != (self.run_id is not None):
            raise ValueError("Steered submissions require an active run")
        return self


class ContinuationAction(StrEnum):
    QUEUED = "queued"
    PREPARED = "prepared"
    NO_TASK = "no_task"


class ContinuationPlan(Contract):
    action: ContinuationAction
    inputs: list[PendingInput] = Field(default_factory=list)
    unresolved_call_ids: list[str] = Field(default_factory=list)
    continuation_of: str | None = None

    @model_validator(mode="after")
    def validate_action(self):
        match self.action:
            case ContinuationAction.QUEUED | ContinuationAction.PREPARED:
                if not self.inputs:
                    raise ValueError("Executable continuation requires inputs")
                if self.action == ContinuationAction.PREPARED:
                    if not self.continuation_of or any(
                        item.continuation_of != self.continuation_of for item in self.inputs
                    ):
                        raise ValueError("Prepared inputs must reference their original run")
                elif self.continuation_of is not None:
                    raise ValueError("Queued continuation cannot declare a new original run")
            case ContinuationAction.NO_TASK:
                if self.inputs or self.continuation_of is not None:
                    raise ValueError("Idle continuation cannot carry executable work")
        return self


class ContextGroupKind(StrEnum):
    USER_REQUEST = "user_request"
    MODEL_STEP = "model_step"
    FACT = "fact"
    SUMMARY = "summary"
    USER_INTERACTION = "user_interaction"


class ContextGroup(Contract):
    start: int = Field(ge=0)
    end: int = Field(ge=1)
    kind: ContextGroupKind
    complete: bool = True
    pending_processes: set[str] = Field(default_factory=set)

    @field_serializer("pending_processes")
    def serialize_pending_processes(self, handles: set[str]) -> list[str]:
        return sorted(handles)

    @model_validator(mode="after")
    def validate_range(self):
        if self.end <= self.start:
            raise ValueError("Context group must contain at least one item")
        return self


class ContextWindow(Contract):
    items: list[NativeItem] = Field(default_factory=list)
    epoch: int = Field(default=0, ge=0)
    source_seq: int = Field(default=0, ge=0)
    groups: list[ContextGroup] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_partition(self):
        if self.groups:
            cursor = 0
            for group in self.groups:
                if group.start != cursor or group.end > len(self.items):
                    raise ValueError("Context groups must partition the item window")
                cursor = group.end
            if cursor != len(self.items):
                raise ValueError("Context groups do not cover the item window")
        return self


class ContextSplit(Contract):
    older: ContextWindow
    current_request: ContextWindow
    tail: ContextWindow


class PrefixSnapshot(Contract):
    version: int = 1
    instructions: str
    tools: list[ToolSpec] = Field(default_factory=list)


class ContextInput(PrefixSnapshot):
    items: list[NativeItem] = Field(default_factory=list)


class ConversationSummary(Contract):
    text: str = Field(min_length=1)

    @field_validator("text")
    @classmethod
    def validate_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Conversation summary must contain nonempty text")
        return stripped


class ModelMetrics(Contract):
    duration_seconds: float = 0
    ttft_seconds: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None


class ModelExchange(Contract):
    response: ModelResponse
    metrics: ModelMetrics


class ModelRequestMetadata(Contract):
    step_id: str
    step: int
    context_epoch: int
    prefix_revision: str
    context_hash: str
    estimated_input_tokens: int
    token_measurement: TokenMeasurement = TokenMeasurement.UTF8_BYTE_ESTIMATE
    cache_key: str


class ModelCompleted(ModelRequestMetadata, ModelMetrics):
    text: str | None = None
    reasoning: list[ReasoningBlock] = Field(default_factory=list)
    calls: list[ToolCall] = Field(default_factory=list)


class ModelCommitted(Contract):
    response: ModelResponse
    metrics: ModelMetrics = Field(default_factory=ModelMetrics)
    step_id: str | None = None


class ModelIncomplete(Contract):
    response: ModelResponse


class ModelFailure(Contract):
    step_id: str
    code: ErrorCode


class NativeUserMessage(NativeMessage):
    role: Literal[MessageRole.USER] = MessageRole.USER
    content: list[NativeContent]

    @model_validator(mode="after")
    def validate_input_content(self):
        if any(part.type != ContentType.INPUT_TEXT for part in self.content):
            raise ValueError("User messages require input text content")
        return self


class UserMessage(Contract):
    item: NativeUserMessage
    command_id: str | None = None


class ToolStateChange(Contract):
    call_id: str
    state: ToolExecutionState
    call: ToolCall | None = None
    request: ApprovalRequest | None = None
    side_effecting: bool = True


class ToolResultCommitted(Contract):
    result: ToolResult
    item: NativeFunctionOutput


class ApprovalDecision(Contract):
    call_id: str
    request_id: str
    approved: bool


class RunStarted(Contract):
    provider: ProviderKind = ProviderKind.OPENAI_RESPONSES
    base_url: str | None = Field(default=None, min_length=1)
    auth_mode: AuthMode | None = None
    status: RunStatus
    model: str
    prefix_revision: str
    instructions: str
    tools: list[ToolSpec]


class RunFinished(Contract):
    status: RunStatus
    text: str = ""
    stop_reason: StopReason | ErrorCode


class BackgroundProcessSettled(Contract):
    call_id: str
    process_handle: str
    result: ProcessResult
    item: NativeUserMessage


class UnknownResolution(Contract):
    call_id: str
    resolution: Literal[ToolStatus.FAILED, ToolStatus.SUCCEEDED]
    note: str
    item: NativeUserMessage


class CompactionStarted(Contract):
    old_epoch: int
    source_seq: int
    reason: CompactionReason


class CompactionCommitted(Contract):
    epoch: int
    source_seq: int
    artifact_id: str
    strategy: ContextStrategy
    strategy_revision: int = 1
    before_tokens: int
    after_tokens: int
    reason: CompactionReason


class EpochChanged(Contract):
    epoch: int


class TextDelta(Contract):
    text: str


class ToolFinished(Contract):
    result: ToolResult


class ErrorOccurred(Contract):
    code: ErrorCode
    message: str


class TruncatedToolOutput(Contract):
    head: str
    tail: str
    truncated: bool = True
    length: int
    artifact_id: str
    status: ToolStatus


class RunProgress(Contract):
    session_id: str
    run_id: str
    status: RunStatus
    stop_reason: StopReason | ErrorCode
    text: str = ""
    tool_count: int = 0
    overflow_retried: bool = False
    dispatched: set[str] = Field(default_factory=set)
    completed: set[str] = Field(default_factory=set)


class RunEnvironment(Contract):
    session_id: str
    run_id: str
    workspace: Path
    instructions: str
    tools: list[ToolSpec]
    prefix_revision: str


class LegacyContextWindow(ContextWindow):
    items: list[NativeMessage | NativeReasoning | NativeFunctionCall | NativeFunctionOutput] = (
        Field(default_factory=list)
    )
