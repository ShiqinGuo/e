import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from http import HTTPStatus
from typing import TypedDict

import httpx
from pydantic import SecretStr

from agent_client.domain.auth import HttpMethod
from agent_client.domain.configuration import ModelConfig
from agent_client.domain.enums import (
    AuthMode,
    ErrorCode,
    ModelEventKind,
    ModelResponseStatus,
    NativeItemType,
    ProviderKind,
    ReasoningChannel,
    ReasoningEffort,
    ReasoningSummary,
)
from agent_client.domain.errors import AgentError
from agent_client.domain.models import (
    ModelEvent,
    ModelRequest,
    ModelResponse,
    ReasoningBlock,
    ToolCall,
)
from agent_client.domain.protocol import (
    ContentType,
    NativeItem,
    NativeMessage,
    NativeReasoning,
    ProtocolObject,
    TokenUsage,
)
from agent_client.domain.provider import (
    FunctionEncoding,
    NamespaceEncoding,
    ProviderErrorCode,
    ProviderErrorEnvelope,
    ProviderResponse,
    ProviderResponseStatus,
    ReasoningOptions,
    ResponseEvent,
    ResponseEventType,
    ResponsesRequest,
)
from agent_client.domain.responses_input import ResponsesInput, ResponsesReasoningInput
from agent_client.infrastructure.auth import AuthService
from agent_client.infrastructure.models.credentials import api_key


@dataclass(frozen=True)
class StreamLimits:
    request_timeout_seconds: float = 120
    connect_timeout_seconds: float = 30
    maximum_bytes: int = 8388608
    delimiter_bytes: int = 1
    data_prefix: str = "data:"
    done_marker: str = "[DONE]"


class AuthorizationHeaders(TypedDict):
    Authorization: str


def provider_error(
    code: str | None,
    status: int | None = None,
    *,
    param: str | None = None,
) -> AgentError:
    failure = classified_provider_error(
        code, status if status is not None else HTTPStatus.BAD_GATEWAY
    )
    details = [f"HTTP {status}"] if status is not None else []
    if code in ProviderErrorCode:
        details.append(f"code={code}")
    if param is not None and re.fullmatch(
        r"(?:input(?:\[\d+\])?(?:\.(?:type|id|status|content|call_id|name|arguments))?|"
        r"reasoning\.(?:effort|summary)|model|tools|store|stream|max_output_tokens)",
        param,
    ):
        details.append(f"param={param}")
    return AgentError(
        failure.code,
        f"{failure.message} ({', '.join(details)})" if details else failure.message,
        retryable=failure.retryable,
    )


def classified_provider_error(code: str | None, status: int) -> AgentError:
    match code:
        case ProviderErrorCode.UNKNOWN_PARAMETER:
            return AgentError(
                ErrorCode.MODEL_UNAVAILABLE, "Model rejected an unknown request parameter"
            )
        case ProviderErrorCode.UNSUPPORTED_VALUE:
            return AgentError(
                ErrorCode.MODEL_UNAVAILABLE,
                "Model rejected an unsupported request parameter value",
            )
        case ProviderErrorCode.CONTEXT_LENGTH_EXCEEDED | ProviderErrorCode.CONTEXT_WINDOW_EXCEEDED:
            return AgentError(
                ErrorCode.CONTEXT_OVERFLOW, "Model input exceeds the available context window"
            )
        case ProviderErrorCode.USAGE_LIMIT | ProviderErrorCode.RATE_LIMIT_EXCEEDED:
            return AgentError(ErrorCode.MODEL_QUOTA_EXHAUSTED, "Model usage limit reached")
        case (
            ProviderErrorCode.INVALID_TOKEN
            | ProviderErrorCode.TOKEN_EXPIRED
            | ProviderErrorCode.INVALID_USER
        ):
            return AgentError(ErrorCode.REAUTH_REQUIRED, "Model authorization required")
        case (
            ProviderErrorCode.MODEL_NOT_FOUND
            | ProviderErrorCode.USER_NOT_ELIGIBLE
            | ProviderErrorCode.ROUTE_NOT_SUPPORTED
            | ProviderErrorCode.SCOPE_NOT_AUTHORIZED
            | ProviderErrorCode.INVALID_AUTHORIZATION_CONTEXT
        ):
            return AgentError(
                ErrorCode.MODEL_ACCESS_DENIED, "Selected model is not available to this account"
            )
    match status:
        case HTTPStatus.BAD_REQUEST:
            return AgentError(ErrorCode.MODEL_UNAVAILABLE, "Model rejected the request")
        case HTTPStatus.UNAUTHORIZED:
            return AgentError(ErrorCode.REAUTH_REQUIRED, "Model authorization required")
        case HTTPStatus.FORBIDDEN:
            return AgentError(
                ErrorCode.MODEL_ACCESS_DENIED, "Selected model is not available to this account"
            )
        case HTTPStatus.TOO_MANY_REQUESTS:
            return AgentError(ErrorCode.MODEL_QUOTA_EXHAUSTED, "Model usage limit reached")
        case _:
            return AgentError(
                ErrorCode.MODEL_UNAVAILABLE,
                "Model service request failed",
                retryable=status >= HTTPStatus.INTERNAL_SERVER_ERROR,
            )


def responses_input(item: NativeItem) -> ResponsesInput:
    match item:
        case NativeReasoning():
            return ResponsesReasoningInput(
                id=item.id,
                summary=item.summary,
                content=item.content or None,
                encrypted_content=item.encrypted_content,
            )
        case _:
            return item


def native_reasoning(item: NativeItem) -> list[ReasoningBlock]:
    if not isinstance(item, NativeReasoning):
        return []
    blocks: list[ReasoningBlock] = []
    for channel, content in (
        (ReasoningChannel.SUMMARY, item.summary),
        (ReasoningChannel.TEXT, item.content),
    ):
        for index, part in enumerate(content):
            if part.type not in {ContentType.SUMMARY_TEXT, ContentType.REASONING_TEXT}:
                raise ValueError("Reasoning item contains invalid visible content")
            if item.id is None:
                raise ValueError("Visible reasoning item identity is missing")
            blocks.append(
                ReasoningBlock(item_id=item.id, index=index, channel=channel, text=part.text)
            )
    return blocks


def completed_response(value: ProviderResponse, emitted: str) -> ModelResponse:
    if value.status != ProviderResponseStatus.COMPLETED or value.id is None:
        raise ValueError("Completion is invalid")
    calls: list[ToolCall] = []
    text: list[str] = []
    identities: set[str] = set()
    for item in value.output:
        if item.status != ProviderResponseStatus.COMPLETED:
            raise ValueError("Output item is incomplete")
        match item.type:
            case NativeItemType.MESSAGE:
                text.extend(
                    block.text or ""
                    for block in item.content
                    if block.type == ContentType.OUTPUT_TEXT
                )
            case NativeItemType.FUNCTION_CALL:
                if item.call_id in identities:
                    raise ValueError("Duplicate tool identity")
                identities.add(item.call_id)
                calls.append(
                    ToolCall(
                        id=item.call_id,
                        name=item.name,
                        arguments=ProtocolObject(item.arguments).wire_value(),
                    )
                )
    final_text = "".join(text)
    if not final_text.startswith(emitted):
        raise ValueError("Stream differs from completed response")
    return ModelResponse(
        id=value.id,
        output=value.output,
        text=final_text,
        reasoning=[block for item in value.output for block in native_reasoning(item)],
        calls=calls,
        usage=value.usage or TokenUsage(),
        status=ModelResponseStatus.COMPLETED,
    )


class ResponsesGateway:
    def __init__(
        self, config: ModelConfig, auth: AuthService, client: httpx.AsyncClient | None = None
    ):
        if config.provider != ProviderKind.OPENAI_RESPONSES:
            raise AgentError(ErrorCode.CONFIG_INVALID, "Unknown model provider")
        if (
            config.auth_mode == AuthMode.CHATGPT
            and config.base_url.rstrip("/") != "https://api.openai.com/v1"
        ):
            raise AgentError(
                ErrorCode.CONFIG_INVALID, "ChatGPT credentials require the official OpenAI endpoint"
            )
        self.config, self.auth = config, auth
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(
                StreamLimits().request_timeout_seconds,
                connect=StreamLimits().connect_timeout_seconds,
            ),
            follow_redirects=False,
        )
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def _token(self) -> SecretStr:
        match self.config.auth_mode:
            case AuthMode.CHATGPT:
                return await self.auth.access_token()
            case AuthMode.API_KEY:
                return api_key(self.config)

    def _body(self, request: ModelRequest) -> ResponsesRequest:
        functions = [
            FunctionEncoding(name=t.name, description=t.description, parameters=t.parameters)
            for t in request.tools
        ]
        api_key = self.config.auth_mode == AuthMode.API_KEY
        return ResponsesRequest(
            model=request.model,
            instructions=request.instructions,
            input=[responses_input(item) for item in request.items],
            reasoning=ReasoningOptions(
                effort=request.reasoning_effort,
                summary=ReasoningSummary.AUTO
                if request.reasoning_effort != ReasoningEffort.NONE
                else None,
            ),
            tools=[NamespaceEncoding(tools=functions)] if functions else None,
            max_output_tokens=request.max_output_tokens if api_key else None,
            prompt_cache_key=request.cache_key if api_key else None,
        )

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        token = await self._token()
        body = self._body(request)
        emitted = ""
        data: list[str] = []
        completed_items: list[tuple[int, NativeItem]] = []
        reasoning: list[ReasoningBlock] = []
        size = 0
        try:
            async with self.client.stream(
                HttpMethod.POST.value,
                self.config.base_url.rstrip("/") + "/responses",
                headers=AuthorizationHeaders(Authorization=f"Bearer {token.get_secret_value()}"),
                follow_redirects=False,
                json=body.model_dump(mode="json", exclude_none=True),
            ) as response:
                if response.status_code != HTTPStatus.OK:
                    await response.aread()
                    try:
                        error = ProviderErrorEnvelope.model_validate(response.json()).error
                    except ValueError:
                        raise AgentError(
                            ErrorCode.MODEL_PROTOCOL,
                            f"Model error response violates the protocol (HTTP {response.status_code})",
                        ) from None
                    raise provider_error(error.code, response.status_code, param=error.param)
                async for line in response.aiter_lines():
                    size += len(line.encode()) + StreamLimits().delimiter_bytes
                    if size > StreamLimits().maximum_bytes:
                        raise AgentError(
                            ErrorCode.MODEL_OUTCOME_UNKNOWN, "Model stream exceeded its size budget"
                        )
                    if line.startswith(StreamLimits().data_prefix):
                        data.append(line[len(StreamLimits().data_prefix) :].lstrip())
                    elif not line and data:
                        payload = "\n".join(data)
                        data.clear()
                        if payload == StreamLimits().done_marker:
                            continue
                        event = ResponseEvent.model_validate_json(payload)
                        match event.type:
                            case (
                                ResponseEventType.REASONING_DELTA
                                | ResponseEventType.REASONING_SUMMARY_DELTA
                                | ResponseEventType.REASONING_DONE
                                | ResponseEventType.REASONING_SUMMARY_DONE
                            ):
                                channel = (
                                    ReasoningChannel.SUMMARY
                                    if event.type
                                    in {
                                        ResponseEventType.REASONING_SUMMARY_DELTA,
                                        ResponseEventType.REASONING_SUMMARY_DONE,
                                    }
                                    else ReasoningChannel.TEXT
                                )
                                index = (
                                    event.summary_index
                                    if channel == ReasoningChannel.SUMMARY
                                    else event.content_index
                                )
                                key = (event.item_id, channel, index)
                                previous = next(
                                    (
                                        block
                                        for block in reasoning
                                        if (block.item_id, block.channel, block.index) == key
                                    ),
                                    None,
                                )
                                previous_text = previous.text if previous else ""
                                if event.type in {
                                    ResponseEventType.REASONING_DELTA,
                                    ResponseEventType.REASONING_SUMMARY_DELTA,
                                }:
                                    text = previous_text + event.delta
                                else:
                                    text = event.text
                                    if not text.startswith(previous_text):
                                        raise ValueError(
                                            "Reasoning stream differs from completed part"
                                        )
                                block = ReasoningBlock(
                                    item_id=event.item_id, channel=channel, index=index, text=text
                                )
                                reasoning = [
                                    existing
                                    for existing in reasoning
                                    if (existing.item_id, existing.channel, existing.index) != key
                                ]
                                reasoning.append(block)
                                if text[len(previous_text) :]:
                                    yield ModelEvent(
                                        kind=ModelEventKind.REASONING_DELTA,
                                        reasoning=ReasoningBlock(
                                            item_id=block.item_id,
                                            channel=block.channel,
                                            index=block.index,
                                            text=text[len(previous_text) :],
                                        ),
                                    )
                            case ResponseEventType.TEXT_DELTA:
                                emitted += event.delta
                                yield ModelEvent(kind=ModelEventKind.TEXT_DELTA, text=event.delta)
                            case ResponseEventType.ITEM_DONE:
                                if any(index == event.output_index for index, _ in completed_items):
                                    raise ValueError("Duplicate completed output item")
                                completed_items.append((event.output_index, event.item))
                            case ResponseEventType.COMPLETED:
                                envelope = event.response
                                if not envelope.output:
                                    if sorted(index for index, _ in completed_items) != list(
                                        range(len(completed_items))
                                    ):
                                        raise ValueError("Completed output contains gaps")
                                    envelope.output = [
                                        item
                                        for _, item in sorted(
                                            completed_items, key=lambda pair: pair[0]
                                        )
                                    ]
                                final = completed_response(envelope, emitted)
                                for block in final.reasoning:
                                    key = (block.item_id, block.channel, block.index)
                                    previous = next(
                                        (
                                            block
                                            for block in reasoning
                                            if (block.item_id, block.channel, block.index) == key
                                        ),
                                        None,
                                    )
                                    previous_text = previous.text if previous else ""
                                    if not block.text.startswith(previous_text):
                                        raise ValueError(
                                            "Reasoning stream differs from completed response"
                                        )
                                    if block.text[len(previous_text) :]:
                                        yield ModelEvent(
                                            kind=ModelEventKind.REASONING_DELTA,
                                            reasoning=ReasoningBlock(
                                                item_id=block.item_id,
                                                channel=block.channel,
                                                index=block.index,
                                                text=block.text[len(previous_text) :],
                                            ),
                                        )
                                    reasoning = [
                                        existing
                                        for existing in reasoning
                                        if (existing.item_id, existing.channel, existing.index)
                                        != key
                                    ]
                                    reasoning.append(block)
                                final.reasoning = reasoning
                                if final.text[len(emitted) :]:
                                    yield ModelEvent(
                                        kind=ModelEventKind.TEXT_DELTA,
                                        text=final.text[len(emitted) :],
                                    )
                                yield ModelEvent(kind=ModelEventKind.COMPLETED, response=final)
                                return
                            case ResponseEventType.INCOMPLETE:
                                envelope = event.response
                                if envelope.id is None:
                                    raise ValueError("Incomplete response identity is missing")
                                partial_text = "".join(
                                    part.text or ""
                                    for item in envelope.output
                                    if isinstance(item, NativeMessage)
                                    for part in item.content
                                    if part.type == ContentType.OUTPUT_TEXT
                                )
                                partial_reasoning = [
                                    block
                                    for item in envelope.output
                                    for block in native_reasoning(item)
                                ]
                                identities = {
                                    (block.item_id, block.channel, block.index)
                                    for block in partial_reasoning
                                }
                                partial_reasoning.extend(
                                    block
                                    for block in reasoning
                                    if (block.item_id, block.channel, block.index) not in identities
                                )
                                yield ModelEvent(
                                    kind=ModelEventKind.COMPLETED,
                                    response=ModelResponse(
                                        id=envelope.id,
                                        output=envelope.output,
                                        text=partial_text or emitted,
                                        reasoning=[
                                            block
                                            for item in envelope.output
                                            for block in native_reasoning(item)
                                        ],
                                        usage=envelope.usage or TokenUsage(),
                                        status=ModelResponseStatus.INCOMPLETE,
                                        incomplete_reason=envelope.incomplete_details.reason
                                        if envelope.incomplete_details
                                        else None,
                                    ),
                                )
                                return
                            case ResponseEventType.FAILED:
                                error = (
                                    event.response.error
                                    if event.response is not None
                                    else event.error
                                )
                                raise provider_error(
                                    error.code if error is not None else None,
                                    param=error.param if error is not None else None,
                                )
                            case ResponseEventType.ERROR:
                                raise provider_error(
                                    event.error.code if event.error is not None else event.code,
                                    param=event.error.param
                                    if event.error is not None
                                    else event.param,
                                )
                            case _:
                                pass
        except (httpx.HTTPError, ValueError, TypeError):
            raise AgentError(
                ErrorCode.MODEL_OUTCOME_UNKNOWN,
                "Model stream interrupted before verified completion",
            ) from None
        raise AgentError(
            ErrorCode.MODEL_OUTCOME_UNKNOWN, "Model stream ended without verified completion"
        )
