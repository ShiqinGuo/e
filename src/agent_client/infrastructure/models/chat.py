from collections.abc import AsyncIterator
from dataclasses import dataclass
from http import HTTPStatus
from typing import TypedDict

import httpx

from agent_client.domain.chat import (
    ChatChunk,
    ChatFinishReason,
    ChatFunction,
    ChatMessage,
    ChatRequest,
    ChatRole,
    ChatStreamOptions,
    ChatThinking,
    ChatTool,
    ChatToolCall,
    ChatToolFunction,
    ChatUsage,
)
from agent_client.domain.configuration import ModelConfig
from agent_client.domain.enums import (
    AuthMode,
    ChatReasoningMode,
    ErrorCode,
    MessageRole,
    ModelEventKind,
    ModelResponseStatus,
    NativeItemType,
    ProviderKind,
    ReasoningChannel,
    ReasoningEffort,
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
    IncompleteReason,
    NativeContent,
    NativeFunctionCall,
    NativeMessage,
    NativeReasoning,
    ProtocolObject,
    ProviderResponseStatus,
    TokenDetails,
    TokenUsage,
)
from agent_client.domain.provider import (
    ProviderErrorCode,
)
from agent_client.infrastructure.models.credentials import api_key
from agent_client.infrastructure.models.responses import provider_error


@dataclass
class ChatCallAccumulator:
    index: int
    id: str = ""
    name: str = ""
    arguments: str = ""


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


def normalized_usage(usage: ChatUsage | None) -> TokenUsage:
    if usage is None:
        return TokenUsage()
    cached = usage.prompt_cache_hit_tokens
    if (
        usage.prompt_tokens_details is not None
        and usage.prompt_tokens_details.cached_tokens is not None
    ):
        cached = usage.prompt_tokens_details.cached_tokens
    prompt_details = (
        TokenDetails(cached_tokens=usage.prompt_tokens_details.cached_tokens)
        if usage.prompt_tokens_details is not None
        else None
    )
    return TokenUsage(
        input_tokens=usage.prompt_tokens,
        output_tokens=usage.completion_tokens,
        total_tokens=usage.total_tokens,
        input_tokens_details=TokenDetails(cached_tokens=cached) if cached is not None else None,
        output_tokens_details=usage.completion_tokens_details,
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        prompt_cache_hit_tokens=usage.prompt_cache_hit_tokens,
        prompt_cache_miss_tokens=usage.prompt_cache_miss_tokens,
        prompt_tokens_details=prompt_details,
        completion_tokens_details=usage.completion_tokens_details,
    )


def chat_messages(request: ModelRequest) -> list[ChatMessage]:
    messages = [ChatMessage(role=ChatRole.SYSTEM, content=request.instructions)]
    assistant: ChatMessage | None = None
    pending_reasoning = ""
    for value in request.items:
        item = value
        match item.type:
            case NativeItemType.REASONING:
                pending_reasoning += "".join(
                    part.text or "" for part in item.content + item.summary
                )
                assistant = None
            case NativeItemType.MESSAGE:
                content = (
                    item.content
                    if isinstance(item.content, str)
                    else "".join(part.text or "" for part in item.content)
                )
                message = ChatMessage(role=ChatRole(item.role.value), content=content)
                if item.role == MessageRole.ASSISTANT:
                    message.reasoning_content = item.reasoning_content or pending_reasoning or None
                    pending_reasoning = ""
                    assistant = message
                else:
                    if pending_reasoning:
                        raise ValueError("Reasoning is not followed by an assistant turn")
                    assistant = None
                messages.append(message)
            case NativeItemType.FUNCTION_CALL:
                call = ChatToolCall(
                    id=item.call_id,
                    function=ChatFunction(name=item.name, arguments=item.arguments),
                )
                if assistant is None:
                    assistant = ChatMessage(
                        role=ChatRole.ASSISTANT,
                        reasoning_content=pending_reasoning or None,
                        tool_calls=[],
                    )
                    pending_reasoning = ""
                    messages.append(assistant)
                if assistant.tool_calls is None:
                    assistant.tool_calls = []
                assistant.tool_calls.append(call)
            case NativeItemType.FUNCTION_CALL_OUTPUT:
                messages.append(
                    ChatMessage(
                        role=ChatRole.TOOL,
                        content=item.output,
                        tool_call_id=item.call_id,
                    )
                )
                assistant = None
    if pending_reasoning:
        raise ValueError("Reasoning is not followed by an assistant turn")
    return messages


class ChatCompletionsGateway:
    def __init__(self, config: ModelConfig, client: httpx.AsyncClient | None = None):
        if (
            config.provider != ProviderKind.OPENAI_CHAT_COMPLETIONS
            or config.auth_mode != AuthMode.API_KEY
        ):
            raise AgentError(
                ErrorCode.CONFIG_INVALID, "Chat Completions requires API key authentication"
            )
        self.config = config
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

    def _body(self, request: ModelRequest) -> ChatRequest:
        thinking = self.config.chat_reasoning
        if (
            request.reasoning_effort == ReasoningEffort.NONE
            and thinking != ChatReasoningMode.DEFAULT
        ):
            thinking = ChatReasoningMode.DISABLED
        return ChatRequest(
            model=request.model,
            messages=chat_messages(request),
            max_tokens=request.max_output_tokens,
            tools=[
                ChatTool(
                    function=ChatToolFunction(
                        name=tool.name,
                        description=tool.description,
                        parameters=tool.parameters,
                    )
                )
                for tool in request.tools
            ]
            or None,
            thinking=ChatThinking(type=thinking) if thinking != ChatReasoningMode.DEFAULT else None,
            reasoning_effort=request.reasoning_effort
            if self.config.chat_send_reasoning_effort
            and request.reasoning_effort != ReasoningEffort.NONE
            else None,
            stream_options=ChatStreamOptions() if self.config.chat_stream_usage else None,
        )

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        token = api_key(self.config)
        try:
            body = self._body(request)
        except (ValueError, TypeError):
            raise AgentError(
                ErrorCode.INVALID_CONTEXT, "Context cannot be encoded as chat messages"
            ) from None
        identifier: str | None = None
        text = ""
        reasoning = ""
        calls: list[ChatCallAccumulator] = []
        usage: ChatUsage | None = None
        finish: ChatFinishReason | None = None
        done = False
        data: list[str] = []
        size = 0
        try:
            async with self.client.stream(
                "POST",
                self.config.base_url.rstrip("/") + "/chat/completions",
                headers=AuthorizationHeaders(Authorization=f"Bearer {token.get_secret_value()}"),
                follow_redirects=False,
                json=body.model_dump(mode="json", exclude_none=True),
            ) as response:
                if response.status_code != HTTPStatus.OK:
                    await response.aread()
                    if response.status_code == HTTPStatus.UNAUTHORIZED:
                        raise AgentError(
                            ErrorCode.MODEL_ACCESS_DENIED, "API key authorization failed (HTTP 401)"
                        )
                    code = ProviderErrorCode.UNSPECIFIED
                    try:
                        error = ChatChunk.model_validate(response.json()).error
                        if error is not None and error.code in ProviderErrorCode:
                            code = ProviderErrorCode(error.code)
                    except (ValueError, TypeError):
                        pass
                    if (
                        response.status_code == HTTPStatus.BAD_REQUEST
                        and code == ProviderErrorCode.UNSPECIFIED
                    ):
                        raise AgentError(
                            ErrorCode.MODEL_PROTOCOL, "Chat request rejected (HTTP 400)"
                        )
                    raise provider_error(code, response.status_code)
                async for line in response.aiter_lines():
                    size += len(line.encode()) + StreamLimits().delimiter_bytes
                    if size > StreamLimits().maximum_bytes:
                        raise ValueError("Stream size exceeded")
                    if line.startswith(StreamLimits().data_prefix):
                        data.append(line[len(StreamLimits().data_prefix) :].lstrip())
                        continue
                    if line or not data:
                        continue
                    payload = "\n".join(data)
                    data.clear()
                    if payload == StreamLimits().done_marker:
                        done = True
                        break
                    chunk = ChatChunk.model_validate_json(payload)
                    if chunk.error is not None:
                        raise provider_error(ProviderErrorCode.UNSPECIFIED)
                    if chunk.id is not None:
                        if identifier is not None and identifier != chunk.id:
                            raise ValueError("Completion identity changed")
                        identifier = chunk.id
                    if chunk.usage is not None:
                        usage = chunk.usage
                    if len(chunk.choices) > 1:
                        raise ValueError("Multiple choices are unsupported")
                    for choice in chunk.choices:
                        if choice.index != 0 or finish is not None:
                            raise ValueError("Unexpected choice after completion")
                        delta = choice.delta
                        if delta.refusal:
                            raise AgentError(
                                ErrorCode.MODEL_INCOMPLETE, "Model response was filtered"
                            )
                        if delta.content:
                            text += delta.content
                            yield ModelEvent(kind=ModelEventKind.TEXT_DELTA, text=delta.content)
                        if delta.reasoning_content:
                            if identifier is None:
                                raise ValueError("Reasoning identity is missing")
                            reasoning += delta.reasoning_content
                            yield ModelEvent(
                                kind=ModelEventKind.REASONING_DELTA,
                                reasoning=ReasoningBlock(
                                    item_id=identifier + "_reasoning",
                                    index=0,
                                    channel=ReasoningChannel.TEXT,
                                    text=delta.reasoning_content,
                                ),
                            )
                        for fragment in delta.tool_calls or []:
                            accumulated = next(
                                (call for call in calls if call.index == fragment.index), None
                            )
                            if accumulated is None:
                                accumulated = ChatCallAccumulator(index=fragment.index)
                                calls.append(accumulated)
                            accumulated.id += fragment.id or ""
                            if fragment.function is not None:
                                accumulated.name += fragment.function.name or ""
                                accumulated.arguments += fragment.function.arguments or ""
                        if choice.finish_reason is not None:
                            finish = choice.finish_reason

            if not done or finish is None or identifier is None:
                raise ValueError("Stream ended without verified completion")
            if finish not in {ChatFinishReason.STOP, ChatFinishReason.TOOL_CALLS}:
                match finish:
                    case ChatFinishReason.LENGTH:
                        incomplete_reason = IncompleteReason.MAX_OUTPUT_TOKENS
                    case ChatFinishReason.CONTENT_FILTER:
                        incomplete_reason = IncompleteReason.CONTENT_FILTER
                    case ChatFinishReason.INSUFFICIENT_RESOURCE:
                        incomplete_reason = IncompleteReason.INSUFFICIENT_RESOURCE

                partial_output = [
                    NativeMessage(
                        id=identifier + "_message",
                        role=MessageRole.ASSISTANT,
                        status=ProviderResponseStatus.INCOMPLETE,
                        content=[NativeContent(type=ContentType.OUTPUT_TEXT, text=text)],
                    )
                ]
                blocks = []
                if reasoning:
                    partial_output.insert(
                        0,
                        NativeReasoning(
                            id=identifier + "_reasoning",
                            status=ProviderResponseStatus.INCOMPLETE,
                            content=[
                                NativeContent(type=ContentType.REASONING_TEXT, text=reasoning)
                            ],
                        ),
                    )
                    blocks.append(
                        ReasoningBlock(
                            item_id=identifier + "_reasoning",
                            index=0,
                            channel=ReasoningChannel.TEXT,
                            text=reasoning,
                        )
                    )
                yield ModelEvent(
                    kind=ModelEventKind.COMPLETED,
                    response=ModelResponse(
                        id=identifier,
                        output=partial_output,
                        text=text,
                        reasoning=blocks,
                        usage=normalized_usage(usage),
                        status=ModelResponseStatus.INCOMPLETE,
                        incomplete_reason=incomplete_reason,
                    ),
                )
                return
            if bool(calls) != (finish == ChatFinishReason.TOOL_CALLS):
                raise ValueError("Tool calls disagree with finish reason")
            if sorted(call.index for call in calls) != list(range(len(calls))):
                raise ValueError("Tool call indices contain gaps")
            final_calls = [
                ToolCall(
                    id=value.id,
                    name=value.name,
                    arguments=ProtocolObject(value.arguments).wire_value(),
                )
                for value in sorted(calls, key=lambda call: call.index)
            ]
            if len({call.id for call in final_calls}) != len(final_calls):
                raise ValueError("Duplicate tool call identity")
            output: list[NativeMessage | NativeReasoning | NativeFunctionCall] = []
            blocks: list[ReasoningBlock] = []
            if reasoning:
                blocks.append(
                    ReasoningBlock(
                        item_id=identifier + "_reasoning",
                        index=0,
                        channel=ReasoningChannel.TEXT,
                        text=reasoning,
                    )
                )
                output.append(
                    NativeReasoning(
                        id=identifier + "_reasoning",
                        content=[NativeContent(type=ContentType.REASONING_TEXT, text=reasoning)],
                    )
                )
            output.append(
                NativeMessage(
                    id=identifier + "_message",
                    role=MessageRole.ASSISTANT,
                    content=[NativeContent(type=ContentType.OUTPUT_TEXT, text=text)],
                )
            )
            output.extend(
                NativeFunctionCall(
                    id=call.id,
                    call_id=call.id,
                    name=call.name,
                    arguments=calls[index].arguments,
                )
                for index, call in enumerate(final_calls)
            )
            final = ModelResponse(
                id=identifier,
                text=text,
                reasoning=blocks,
                calls=final_calls,
                output=output,
                usage=normalized_usage(usage),
            )
        except (httpx.HTTPError, ValueError, TypeError):
            raise AgentError(
                ErrorCode.MODEL_OUTCOME_UNKNOWN,
                "Model stream interrupted before verified completion",
            ) from None
        yield ModelEvent(kind=ModelEventKind.COMPLETED, response=final)
