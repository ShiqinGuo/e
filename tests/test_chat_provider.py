import json

import httpx
import pytest

from agent_client.domain.chat import (
    ChatChoice,
    ChatChunk,
    ChatDelta,
    ChatFinishReason,
    ChatFunctionDelta,
    ChatTokenDetails,
    ChatToolDelta,
    ChatUsage,
)
from agent_client.domain.configuration import ModelConfig
from agent_client.domain.enums import (
    AuthMode,
    ChatReasoningMode,
    ErrorCode,
    ModelEventKind,
    ModelResponseStatus,
    NativeItemType,
    ProviderKind,
    ReasoningEffort,
)
from agent_client.domain.errors import AgentError
from agent_client.domain.models import ModelRequest, ToolSpec
from agent_client.domain.protocol import NATIVE_ITEM_ADAPTER, IncompleteReason, NativeFunctionOutput
from agent_client.infrastructure.models.chat import ChatCompletionsGateway


def config(
    chat_reasoning: ChatReasoningMode = ChatReasoningMode.DEFAULT,
    chat_stream_usage: bool = False,
    chat_send_reasoning_effort: bool = False,
) -> ModelConfig:
    return ModelConfig(
        provider=ProviderKind.OPENAI_CHAT_COMPLETIONS,
        auth_mode=AuthMode.API_KEY,
        base_url="https://example.test/v1",
        model="deepseek-test",
        chat_reasoning=chat_reasoning,
        chat_stream_usage=chat_stream_usage,
        chat_send_reasoning_effort=chat_send_reasoning_effort,
    )


def request() -> ModelRequest:
    return ModelRequest(
        model="deepseek-test",
        instructions="Help",
        cache_key="test",
        items=[{"type": NativeItemType.MESSAGE, "role": "user", "content": "hello"}],
        tools=[ToolSpec(name="read_file", description="Read", parameters={"type": "object"})],
    )


def chunk(delta: ChatDelta, finish: ChatFinishReason | None = None) -> ChatChunk:
    return ChatChunk(
        id="completion", choices=[ChatChoice(index=0, delta=delta, finish_reason=finish)]
    )


def sse(chunks: list[ChatChunk], done: bool = True) -> str:
    return "".join(
        "data: " + value.model_dump_json(exclude_none=True) + "\n\n" for value in chunks
    ) + ("data: [DONE]\n\n" if done else "")


@pytest.mark.asyncio
async def test_reasoning_tool_fragments_and_replay(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-secret")
    chunks = [
        chunk(ChatDelta(reasoning_content="Consider ")),
        chunk(
            ChatDelta(
                reasoning_content="the file",
                content="Reading",
                tool_calls=[
                    ChatToolDelta(
                        index=0,
                        id="call_",
                        function=ChatFunctionDelta(name="read_", arguments='{"path":'),
                    )
                ],
            )
        ),
        chunk(
            ChatDelta(
                tool_calls=[
                    ChatToolDelta(
                        index=0, id="1", function=ChatFunctionDelta(name="file", arguments='"a"}')
                    )
                ]
            ),
            ChatFinishReason.TOOL_CALLS,
        ),
        ChatChunk(
            id="completion", usage=ChatUsage(prompt_tokens=12, completion_tokens=5, total_tokens=17)
        ),
    ]
    bodies = []

    def handle(incoming: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(incoming.content))
        assert str(incoming.url) == "https://example.test/v1/chat/completions"
        assert incoming.headers["Authorization"] == "Bearer test-secret"
        assert "ChatGPT-Account-Id" not in incoming.headers
        return httpx.Response(200, text=sse(chunks))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        gateway = ChatCompletionsGateway(
            config(chat_reasoning=ChatReasoningMode.ENABLED, chat_stream_usage=True), client
        )
        events = [event async for event in gateway.stream(request())]
        final = events[-1].response
        assert events[-1].kind == ModelEventKind.COMPLETED
        assert final.calls[0].name == "read_file"
        assert final.calls[0].id == "call_1"
        assert final.calls[0].arguments.path == "a"
        assert final.reasoning[0].text == "Consider the file"
        assert final.usage.input_tokens == 12
        following = request()
        following.items.extend(final.output)
        following.items.append(NativeFunctionOutput(call_id="call_1", output="data"))
        messages = gateway._body(following).messages
        assert messages[2].reasoning_content == "Consider the file"
        assert messages[2].tool_calls[0].id == "call_1"
        assert messages[3].tool_call_id == "call_1"
        assert bodies[0]["thinking"] == {"type": "enabled"}
        assert bodies[0]["stream_options"] == {"include_usage": True}
        assert "reasoning_effort" not in bodies[0]
        assert "prompt_cache_key" not in bodies[0]


@pytest.mark.asyncio
async def test_unknown_usage_and_generic_request(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-secret")
    bodies = []

    def handle(incoming: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(incoming.content))
        return httpx.Response(
            200, text=sse([chunk(ChatDelta(content="ok"), ChatFinishReason.STOP)])
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        events = [
            event async for event in ChatCompletionsGateway(config(), client).stream(request())
        ]
    assert events[-1].response.usage.input_tokens is None
    assert events[-1].response.text == "ok"
    assert "thinking" not in bodies[0]
    assert "stream_options" not in bodies[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "finish,done,code",
    [
        (None, False, ErrorCode.MODEL_OUTCOME_UNKNOWN),
        (None, True, ErrorCode.MODEL_OUTCOME_UNKNOWN),
        (ChatFinishReason.STOP, False, ErrorCode.MODEL_OUTCOME_UNKNOWN),
        (ChatFinishReason.TOOL_CALLS, True, ErrorCode.MODEL_OUTCOME_UNKNOWN),
    ],
)
async def test_unverified_or_incomplete_stream_never_completes(monkeypatch, finish, done, code):
    monkeypatch.setenv("OPENAI_API_KEY", "test-secret")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, text=sse([chunk(ChatDelta(content="partial"), finish)], done)
            )
        )
    ) as client:
        events = []
        with pytest.raises(AgentError) as error:
            async for event in ChatCompletionsGateway(config(), client).stream(request()):
                events.append(event)
    assert error.value.code == code
    assert all(event.kind != ModelEventKind.COMPLETED for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,code",
    [
        (401, ErrorCode.MODEL_ACCESS_DENIED),
        (400, ErrorCode.MODEL_PROTOCOL),
        (403, ErrorCode.MODEL_ACCESS_DENIED),
        (429, ErrorCode.MODEL_QUOTA_EXHAUSTED),
        (500, ErrorCode.MODEL_UNAVAILABLE),
        (302, ErrorCode.MODEL_UNAVAILABLE),
    ],
)
async def test_http_failure_does_not_expose_secrets(monkeypatch, status, code):
    monkeypatch.setenv("OPENAI_API_KEY", "test-secret")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                status, text="test-secret", headers={"location": "https://other.test"}
            )
        ),
        follow_redirects=True,
    ) as client:
        with pytest.raises(AgentError) as error:
            _ = [
                event async for event in ChatCompletionsGateway(config(), client).stream(request())
            ]
    assert error.value.code == code
    assert "test-secret" not in str(error.value)


@pytest.mark.asyncio
async def test_missing_key_prevents_request(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    def handle(incoming: httpx.Request) -> httpx.Response:
        raise AssertionError("No request expected")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(AgentError) as error:
            _ = [
                event async for event in ChatCompletionsGateway(config(), client).stream(request())
            ]
    assert error.value.code == ErrorCode.API_KEY_MISSING


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments", ['{"path":', "[]", "null", '"value"', '{"path":"a","path":"b"}']
)
async def test_invalid_tool_arguments_never_dispatch(monkeypatch, arguments):
    monkeypatch.setenv("OPENAI_API_KEY", "test-secret")
    value = chunk(
        ChatDelta(
            tool_calls=[
                ChatToolDelta(
                    index=0,
                    id="call",
                    function=ChatFunctionDelta(name="read_file", arguments=arguments),
                )
            ]
        ),
        ChatFinishReason.TOOL_CALLS,
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text=sse([value])))
    ) as client:
        with pytest.raises(AgentError) as error:
            _ = [
                event async for event in ChatCompletionsGateway(config(), client).stream(request())
            ]
    assert error.value.code == ErrorCode.MODEL_OUTCOME_UNKNOWN


def test_final_answer_reasoning_replayed_before_new_user_turn():
    from agent_client.infrastructure.models.chat import chat_messages

    value = request()
    value.items.extend(
        [
            {"type": "reasoning", "content": [{"type": "reasoning_text", "text": "Thought"}]},
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Answer"}],
            },
            {"type": "message", "role": "user", "content": "Follow up"},
        ]
    )
    value.items = [NATIVE_ITEM_ADAPTER.validate_python(item) for item in value.items]
    messages = chat_messages(value)
    assert messages[2].reasoning_content == "Thought"
    assert messages[2].content == "Answer"
    assert messages[3].content == "Follow up"


@pytest.mark.parametrize(
    "effort,thinking,expected",
    [
        (ReasoningEffort.NONE, ChatReasoningMode.ENABLED, ChatReasoningMode.DISABLED),
        (ReasoningEffort.HIGH, ChatReasoningMode.ENABLED, ChatReasoningMode.ENABLED),
        (ReasoningEffort.MAX, ChatReasoningMode.ENABLED, ChatReasoningMode.ENABLED),
        (ReasoningEffort.NONE, ChatReasoningMode.DISABLED, ChatReasoningMode.DISABLED),
    ],
)
def test_explicit_reasoning_effort_and_off(effort, thinking, expected):
    gateway = ChatCompletionsGateway(
        config(chat_reasoning=thinking, chat_send_reasoning_effort=True)
    )
    value = request()
    value.reasoning_effort = effort
    body = gateway._body(value).model_dump(mode="json", exclude_none=True)
    assert body["thinking"] == {"type": expected}
    if effort == ReasoningEffort.NONE:
        assert "reasoning_effort" not in body
    else:
        assert body["reasoning_effort"] == effort


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "usage,expected",
    [
        (ChatUsage(prompt_cache_hit_tokens=12), 12),
        (ChatUsage(prompt_tokens_details=ChatTokenDetails(cached_tokens=4)), 4),
        (
            ChatUsage(
                prompt_cache_hit_tokens=12, prompt_tokens_details=ChatTokenDetails(cached_tokens=4)
            ),
            4,
        ),
        (ChatUsage(prompt_cache_hit_tokens=0), 0),
        (ChatUsage(prompt_tokens=12), None),
    ],
)
async def test_cache_usage_normalized_without_fabricating_unknown(monkeypatch, usage, expected):
    monkeypatch.setenv("OPENAI_API_KEY", "test-secret")
    chunks = [
        chunk(ChatDelta(content="ok"), ChatFinishReason.STOP),
        ChatChunk(id="completion", usage=usage),
    ]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text=sse(chunks)))
    ) as client:
        events = [
            event async for event in ChatCompletionsGateway(config(), client).stream(request())
        ]
    result = events[-1].response.usage
    if expected is None:
        assert result.input_tokens_details is None
    else:
        assert result.input_tokens_details.cached_tokens == expected


@pytest.mark.asyncio
async def test_length_finished_chat_preserves_partial_usage_without_executable_calls(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-secret")
    chunks = [
        chunk(
            ChatDelta(content="partial", reasoning_content="partial thought"),
            ChatFinishReason.LENGTH,
        ),
        ChatChunk(id="completion", usage=ChatUsage(prompt_tokens=12, completion_tokens=5)),
    ]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text=sse(chunks)))
    ) as client:
        events = [
            event async for event in ChatCompletionsGateway(config(), client).stream(request())
        ]
    response = events[-1].response
    assert response.status == ModelResponseStatus.INCOMPLETE
    assert response.calls == []
    assert response.text == "partial"
    assert response.reasoning[0].text == "partial thought"
    assert response.usage.output_tokens == 5
    assert response.incomplete_reason == IncompleteReason.MAX_OUTPUT_TOKENS
