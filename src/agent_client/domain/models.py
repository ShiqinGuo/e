from collections.abc import Awaitable, Callable
from pathlib import Path

from pydantic import Field, ValidationInfo, field_validator, model_validator

from agent_client.domain.base import Contract, Effect
from agent_client.domain.enums import (
    ApprovalMode,
    ErrorCode,
    ModelEventKind,
    ModelResponseStatus,
    ReasoningChannel,
    ReasoningEffort,
    RunStatus,
    StopReason,
    ToolStatus,
)
from agent_client.domain.protocol import IncompleteReason, NativeItem, ProtocolObject, TokenUsage
from agent_client.domain.tools import ToolArguments, ToolContent, ToolName, argument_model


class ToolSpec(Contract):
    name: str = Field(min_length=1)
    description: str
    parameters: ProtocolObject
    effect: Effect = Effect.READ


class ToolCall(Contract):
    id: str = Field(min_length=1)
    name: ToolName
    arguments: ToolArguments

    @field_validator("arguments", mode="before")
    @classmethod
    def parse_arguments(cls, value: object, info: ValidationInfo):
        if "name" not in info.data:
            raise ValueError("Tool name must be valid before parsing arguments")
        return argument_model(info.data["name"]).model_validate(value)


class ToolResult(Contract):
    call_id: str = Field(min_length=1)
    content: ToolContent
    is_error: bool = False
    status: ToolStatus = ToolStatus.SUCCEEDED
    artifact_id: str | None = None


class ApprovalRequest(Contract):
    id: str = Field(min_length=1)
    call_id: str = Field(min_length=1)
    tool: ToolName
    description: str
    arguments: ToolArguments

    @field_validator("arguments", mode="before")
    @classmethod
    def parse_arguments(cls, value: object, info: ValidationInfo):
        if "tool" not in info.data:
            raise ValueError("Tool name must be valid before parsing approval arguments")
        return argument_model(info.data["tool"]).model_validate(value)


type ApprovalHandler = Callable[[ApprovalRequest], Awaitable[bool]]


class ToolContext(Contract):
    approval_mode: ApprovalMode = ApprovalMode.ASK
    workspace: Path
    home: Path
    session_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    allow_write: bool = False
    allow_commands: bool = False
    unresolved_call_ids: list[str] = Field(default_factory=list)


class ModelRequest(Contract):
    model: str = Field(min_length=1)
    instructions: str
    items: list[NativeItem]
    tools: list[ToolSpec] = Field(default_factory=list)
    cache_key: str = Field(min_length=1)
    max_output_tokens: int = Field(default=8192, ge=1)
    reasoning_effort: ReasoningEffort = ReasoningEffort.MEDIUM


class ReasoningBlock(Contract):
    item_id: str = Field(min_length=1)
    index: int = Field(ge=0)
    channel: ReasoningChannel
    text: str


class ModelResponse(Contract):
    id: str = Field(min_length=1)
    output: list[NativeItem]
    text: str = ""
    reasoning: list[ReasoningBlock] = Field(default_factory=list)
    calls: list[ToolCall] = Field(default_factory=list)
    usage: TokenUsage = Field(default_factory=TokenUsage)
    status: ModelResponseStatus = ModelResponseStatus.COMPLETED
    incomplete_reason: IncompleteReason | None = None


class ModelEvent(Contract):
    kind: ModelEventKind
    text: str = ""
    reasoning: ReasoningBlock | None = None
    response: ModelResponse | None = None

    @model_validator(mode="after")
    def validate_payload(self):
        match self.kind:
            case ModelEventKind.REASONING_DELTA:
                if self.reasoning is None or self.response is not None or self.text:
                    raise ValueError("A reasoning delta requires only reasoning content")
            case ModelEventKind.COMPLETED if self.response is None:
                raise ValueError("A completed model event requires a response")
            case ModelEventKind.TEXT_DELTA if self.response is not None:
                raise ValueError("A text delta cannot contain a completed response")
        if self.kind != ModelEventKind.REASONING_DELTA and self.reasoning is not None:
            raise ValueError("Reasoning content requires a reasoning delta")
        return self


class SessionInfo(Contract):
    id: str = Field(min_length=1)
    workspace: str = Field(min_length=1)
    title: str = "New session"
    status: RunStatus = RunStatus.IDLE
    seq: int = Field(default=0, ge=0)


class RunResult(Contract):
    session_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    status: RunStatus
    text: str = ""
    stop_reason: StopReason | ErrorCode = StopReason.COMPLETED
