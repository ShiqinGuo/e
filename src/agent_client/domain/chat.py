from enum import StrEnum
from typing import Literal

from pydantic import ConfigDict, Field

from agent_client.domain.base import Contract
from agent_client.domain.enums import (
    ChatReasoningMode,
    ReasoningEffort,
)
from agent_client.domain.protocol import ProtocolObject, TokenDetails
from agent_client.domain.provider import ToolEncodingType


class ChatRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
    DEVELOPER = "developer"


class ChatFinishReason(StrEnum):
    STOP = "stop"
    TOOL_CALLS = "tool_calls"
    LENGTH = "length"
    CONTENT_FILTER = "content_filter"
    INSUFFICIENT_RESOURCE = "insufficient_system_resource"


class ChatFunction(Contract):
    name: str = Field(min_length=1)
    arguments: str


class ChatToolCall(Contract):
    id: str = Field(min_length=1)
    type: Literal[ToolEncodingType.FUNCTION] = ToolEncodingType.FUNCTION
    function: ChatFunction


class ChatMessage(Contract):
    role: ChatRole
    content: str | None = None
    reasoning_content: str | None = None
    tool_calls: list[ChatToolCall] | None = None
    tool_call_id: str | None = None


class ChatToolFunction(Contract):
    name: str
    description: str
    parameters: ProtocolObject


class ChatTool(Contract):
    type: Literal[ToolEncodingType.FUNCTION] = ToolEncodingType.FUNCTION
    function: ChatToolFunction


class ChatThinking(Contract):
    type: Literal[ChatReasoningMode.ENABLED, ChatReasoningMode.DISABLED]


class ChatStreamOptions(Contract):
    include_usage: bool = True


class ChatRequest(Contract):
    model: str
    messages: list[ChatMessage]
    stream: bool = True
    max_tokens: int = Field(ge=1)
    tools: list[ChatTool] | None = None
    thinking: ChatThinking | None = None
    reasoning_effort: ReasoningEffort | None = None
    stream_options: ChatStreamOptions | None = None


class ChatFunctionDelta(Contract):
    model_config = ConfigDict(extra="ignore")
    name: str | None = None
    arguments: str | None = None


class ChatToolDelta(Contract):
    model_config = ConfigDict(extra="ignore")
    index: int = Field(ge=0, strict=True)
    id: str | None = None
    type: Literal[ToolEncodingType.FUNCTION] | None = None
    function: ChatFunctionDelta | None = None


class ChatDelta(Contract):
    model_config = ConfigDict(extra="ignore")
    role: Literal[ChatRole.ASSISTANT] | None = None
    content: str | None = None
    reasoning_content: str | None = None
    tool_calls: list[ChatToolDelta] | None = None
    refusal: str | None = None


class ChatChoice(Contract):
    model_config = ConfigDict(extra="ignore")
    index: int = Field(ge=0, strict=True)
    delta: ChatDelta
    finish_reason: ChatFinishReason | None = None


class ChatTokenDetails(Contract):
    model_config = ConfigDict(extra="forbid")
    cached_tokens: int | None = Field(default=None, ge=0, strict=True)


class ChatUsage(Contract):
    model_config = ConfigDict(extra="forbid")
    prompt_tokens: int | None = Field(default=None, ge=0, strict=True)
    completion_tokens: int | None = Field(default=None, ge=0, strict=True)
    total_tokens: int | None = Field(default=None, ge=0, strict=True)
    prompt_cache_hit_tokens: int | None = Field(default=None, ge=0, strict=True)
    prompt_cache_miss_tokens: int | None = Field(default=None, ge=0, strict=True)
    prompt_tokens_details: ChatTokenDetails | None = None
    completion_tokens_details: TokenDetails | None = None


class ChatError(Contract):
    model_config = ConfigDict(extra="ignore")
    code: str | None = None


class ChatChunk(Contract):
    model_config = ConfigDict(extra="ignore")
    id: str | None = Field(default=None, min_length=1)
    choices: list[ChatChoice] = Field(default_factory=list)
    usage: ChatUsage | None = None
    error: ChatError | None = None
