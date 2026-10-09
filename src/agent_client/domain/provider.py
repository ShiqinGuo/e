from enum import StrEnum
from typing import Literal

from pydantic import ConfigDict, Field, model_validator

from agent_client.domain.base import Contract
from agent_client.domain.enums import (
    MessageRole,
    ReasoningEffort,
    ReasoningSummary,
)
from agent_client.domain.protocol import (
    FunctionNamespace,
    IncompleteDetails,
    NativeItem,
    NativeMessage,
    ProtocolObject,
    ProviderResponseStatus,
    TokenUsage,
)
from agent_client.domain.responses_input import ResponsesInput


class ProviderErrorCode(StrEnum):
    CONTEXT_LENGTH_EXCEEDED = "context_length_exceeded"
    CONTEXT_WINDOW_EXCEEDED = "context_window_exceeded"
    USAGE_LIMIT = "subscription_sharing_usage_limit_exceeded"
    INVALID_TOKEN = "invalid_token"
    TOKEN_EXPIRED = "token_expired"
    INVALID_USER = "subscription_sharing_invalid_user"
    MODEL_NOT_FOUND = "model_not_found"
    USER_NOT_ELIGIBLE = "subscription_sharing_user_not_eligible"
    ROUTE_NOT_SUPPORTED = "subscription_sharing_route_not_supported"
    SCOPE_NOT_AUTHORIZED = "chatpass_v2_scope_not_authorized"
    INVALID_AUTHORIZATION_CONTEXT = "chatpass_v2_invalid_authorization_context"
    SERVER_ERROR = "server_error"
    RATE_LIMIT_EXCEEDED = "rate_limit_exceeded"
    UNSUPPORTED_VALUE = "unsupported_value"
    UNKNOWN_PARAMETER = "unknown_parameter"
    UNSPECIFIED = "unspecified"


class ResponseEventType(StrEnum):
    TEXT_DELTA = "response.output_text.delta"
    ITEM_DONE = "response.output_item.done"
    COMPLETED = "response.completed"
    INCOMPLETE = "response.incomplete"
    FAILED = "response.failed"
    ERROR = "error"
    CREATED = "response.created"
    IN_PROGRESS = "response.in_progress"
    QUEUED = "response.queued"
    ANNOTATION_ADDED = "response.output_text.annotation.added"
    ITEM_ADDED = "response.output_item.added"
    CONTENT_ADDED = "response.content_part.added"
    CONTENT_DONE = "response.content_part.done"
    TEXT_DONE = "response.output_text.done"
    ARGUMENTS_DELTA = "response.function_call_arguments.delta"
    ARGUMENTS_DONE = "response.function_call_arguments.done"
    REASONING_DELTA = "response.reasoning_text.delta"
    REASONING_DONE = "response.reasoning_text.done"
    REASONING_SUMMARY_DELTA = "response.reasoning_summary_text.delta"
    REASONING_SUMMARY_DONE = "response.reasoning_summary_text.done"
    REASONING_PART_ADDED = "response.reasoning_summary_part.added"
    REASONING_PART_DONE = "response.reasoning_summary_part.done"
    REFUSAL_DELTA = "response.refusal.delta"
    REFUSAL_DONE = "response.refusal.done"


class ToolEncodingType(StrEnum):
    FUNCTION = "function"
    NAMESPACE = "namespace"


class ProviderResponse(Contract):
    model_config = ConfigDict(extra="ignore")
    id: str | None = None
    status: ProviderResponseStatus = ProviderResponseStatus.COMPLETED
    output: list[NativeItem] = Field(default_factory=list)
    usage: TokenUsage | None = None
    error: "ProviderError | None" = None
    incomplete_details: IncompleteDetails | None = None

    @model_validator(mode="after")
    def validate_output(self):
        for item in self.output:
            if isinstance(item, NativeMessage) and (
                item.role != MessageRole.ASSISTANT or isinstance(item.content, str)
            ):
                raise ValueError("Provider output requires assistant content parts")
        return self


class ProviderError(Contract):
    model_config = ConfigDict(extra="ignore")
    code: str | None = None
    message: str | None = None
    param: str | None = None
    type: str | None = None

    @model_validator(mode="after")
    def validate_error(self):
        if self.code is None and self.message is None:
            raise ValueError("Provider error details are missing")
        return self


class ProviderErrorEnvelope(Contract):
    model_config = ConfigDict(extra="ignore")
    error: ProviderError


class ResponseEvent(Contract):
    model_config = ConfigDict(extra="ignore")
    type: ResponseEventType
    delta: str | None = None
    text: str | None = None
    item_id: str | None = Field(default=None, min_length=1)
    summary_index: int | None = Field(default=None, ge=0)
    content_index: int | None = Field(default=None, ge=0)
    output_index: int | None = Field(default=None, ge=0)
    item: NativeItem | None = None
    response: ProviderResponse | None = None
    error: ProviderError | None = None
    code: str | None = None
    message: str | None = None
    param: str | None = None

    @model_validator(mode="after")
    def validate_event(self):
        match self.type:
            case (
                ResponseEventType.REASONING_DELTA
                | ResponseEventType.REASONING_SUMMARY_DELTA
                | ResponseEventType.REASONING_DONE
                | ResponseEventType.REASONING_SUMMARY_DONE
            ):
                if self.item_id is None:
                    raise ValueError("Reasoning item identity is missing")
                summary = self.type in {
                    ResponseEventType.REASONING_SUMMARY_DELTA,
                    ResponseEventType.REASONING_SUMMARY_DONE,
                }
                if (self.summary_index if summary else self.content_index) is None:
                    raise ValueError("Reasoning part index is missing")
                delta = self.type in {
                    ResponseEventType.REASONING_DELTA,
                    ResponseEventType.REASONING_SUMMARY_DELTA,
                }
                if (self.delta if delta else self.text) is None:
                    raise ValueError("Reasoning text is missing")
            case ResponseEventType.TEXT_DELTA:
                if self.delta is None:
                    raise ValueError("Text delta is missing")
            case ResponseEventType.ITEM_DONE:
                if self.item is None or self.output_index is None:
                    raise ValueError("Completed output item is missing")
                if self.item.status != ProviderResponseStatus.COMPLETED:
                    raise ValueError("Completed output item is not complete")
            case ResponseEventType.COMPLETED:
                if self.response is None or self.response.id is None:
                    raise ValueError("Completed response identity is missing")
            case ResponseEventType.FAILED:
                if self.response is None or self.response.error is None:
                    raise ValueError("Failed response error is missing")
            case ResponseEventType.INCOMPLETE:
                if self.response is None:
                    raise ValueError("Incomplete response is missing")
            case ResponseEventType.ERROR:
                if self.error is None and self.code is None and self.message is None:
                    raise ValueError("Stream error details are missing")
        return self


class FunctionEncoding(Contract):
    type: Literal[ToolEncodingType.FUNCTION] = ToolEncodingType.FUNCTION
    name: str
    description: str
    parameters: ProtocolObject


class NamespaceEncoding(Contract):
    type: Literal[ToolEncodingType.NAMESPACE] = ToolEncodingType.NAMESPACE
    name: FunctionNamespace = FunctionNamespace.FUNCTIONS
    description: str = "Application tools"
    tools: list[FunctionEncoding]


class ReasoningOptions(Contract):
    effort: ReasoningEffort
    summary: ReasoningSummary | None = ReasoningSummary.AUTO


class ResponsesRequest(Contract):
    model: str
    instructions: str
    input: list[ResponsesInput]
    store: bool = False
    stream: bool = True
    reasoning: ReasoningOptions
    tools: list[NamespaceEncoding] | None = None
    max_output_tokens: int | None = None
    prompt_cache_key: str | None = None
