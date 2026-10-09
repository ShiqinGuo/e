import json
from typing import TypedDict

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from agent_client.domain.auth import AuthState
from agent_client.domain.configuration import ModelConfig
from agent_client.domain.enums import (
    AuthMode,
    CompactionReason,
    ErrorCode,
    JournalEventType,
    MessageRole,
    ModelEventKind,
    ModelResponseStatus,
    NativeItemType,
    ReasoningChannel,
)
from agent_client.domain.errors import AgentError
from agent_client.domain.models import ModelRequest
from agent_client.domain.protocol import ContentType, FunctionNamespace, ProviderResponseStatus
from agent_client.domain.provider import (
    ProviderErrorCode,
    ResponseEvent,
    ResponseEventType,
)
from agent_client.infrastructure.models import ResponsesGateway


class ContentFixture(TypedDict):
    type: ContentType
    text: str


class NativeFixture(TypedDict, total=False):
    id: str
    type: NativeItemType
    status: ModelResponseStatus
    call_id: str
    name: str
    arguments: str
    namespace: FunctionNamespace
    role: MessageRole
    content: list[ContentFixture]
    summary: list[ContentFixture]
    encrypted_content: str


class ErrorFixture(TypedDict, total=False):
    code: str | None
    message: str
    param: str | None
    type: str


class ResponseFixture(TypedDict, total=False):
    id: str
    status: ModelResponseStatus | ProviderResponseStatus
    output: list[NativeFixture]
    error: ErrorFixture


class EventFixture(TypedDict, total=False):
    type: ResponseEventType
    output_index: int
    item: NativeFixture
    response: ResponseFixture
    error: ErrorFixture
    code: str | None
    message: str
    sequence_number: int
    item_id: str
    delta: str
    text: str
    summary_index: int
    content_index: int
    annotation_index: int
    annotation: None


class Auth:
    async def access_token(self):
        return SecretStr("secret")


class Fragments(httpx.AsyncByteStream):
    def __init__(self, events: list[EventFixture]):
        self.raw = "".join(("data: " + json.dumps(e) + "\r\n\r\n" for e in events)).encode()

    async def __aiter__(self):
        for start in range(0, len(self.raw), 7):
            yield self.raw[start : start + 7]


def request():
    return ModelRequest(
        model="gpt-6.1-sol", instructions="Help", items=[], cache_key="stable-session"
    )


async def test_fragmented_sse_completed_items_preserve_native_output():
    item = {
        "type": NativeItemType.FUNCTION_CALL,
        "status": ModelResponseStatus.COMPLETED,
        "call_id": "call1",
        "namespace": FunctionNamespace.FUNCTIONS,
        "name": "read_file",
        "arguments": '{"path":"a"}',
    }
    events = [
        {
            "type": ResponseEventType.QUEUED,
            "sequence_number": 1,
            "response": {"id": "r", "status": ProviderResponseStatus.QUEUED, "output": []},
        },
        {
            "type": ResponseEventType.ANNOTATION_ADDED,
            "sequence_number": 2,
            "item_id": "message",
            "output_index": 0,
            "content_index": 0,
            "annotation_index": 0,
            "annotation": None,
        },
        {"type": ResponseEventType.ITEM_DONE, "output_index": 0, "item": item},
        {
            "type": ResponseEventType.COMPLETED,
            "response": {"id": "r", "status": ModelResponseStatus.COMPLETED, "output": []},
        },
    ]

    def handler(req):
        body = json.loads(req.content)
        assert body["store"] is False and body["stream"] is True
        assert "prompt_cache_key" not in body
        return httpx.Response(200, stream=Fragments(events))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        gateway = ResponsesGateway(ModelConfig(), Auth(), client)
        result = [e async for e in gateway.stream(request())]
        assert result[-1].response.output[0].model_dump(mode="json", exclude_unset=True) == item
        assert result[-1].response.calls[0].arguments.path == "a"


@pytest.mark.parametrize("terminal", [None, ResponseEventType.INCOMPLETE])
async def test_incomplete_stream_never_emits_executable_calls(terminal):
    events = [
        {
            "type": ResponseEventType.ITEM_DONE,
            "output_index": 0,
            "item": {
                "type": NativeItemType.FUNCTION_CALL,
                "call_id": "c",
                "name": "write",
                "arguments": "{}",
            },
        }
    ]
    if terminal:
        events.append({"type": terminal})
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=Fragments(events)))
    ) as client:
        gateway = ResponsesGateway(ModelConfig(), Auth(), client)
        emitted = []
        with pytest.raises(AgentError):
            async for event in gateway.stream(request()):
                emitted.append(event)
        assert not any((e.response for e in emitted))


async def test_api_key_explicit_and_stable_cache_key(monkeypatch):
    monkeypatch.setenv("CUSTOM_KEY", "key")

    def handler(req):
        assert req.headers["authorization"] == "Bearer key"
        assert json.loads(req.content)["prompt_cache_key"] == "stable-session"
        return httpx.Response(
            200,
            stream=Fragments(
                [{"type": ResponseEventType.COMPLETED, "response": {"id": "r", "output": []}}]
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        gateway = ResponsesGateway(
            ModelConfig(auth_mode=AuthMode.API_KEY, api_key_env="CUSTOM_KEY"), Auth(), client
        )
        assert [e async for e in gateway.stream(request())][
            -1
        ].kind == ModelResponseStatus.COMPLETED


def test_subscription_token_cannot_target_custom_endpoint():
    with pytest.raises((AgentError, ValueError)):
        ResponsesGateway(ModelConfig(base_url="https://example.com"), Auth())


async def test_provider_body_never_leaks_into_error():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                401,
                json={
                    ResponseEventType.ERROR: {
                        "code": ProviderErrorCode.INVALID_TOKEN,
                        NativeItemType.MESSAGE: "secret",
                    }
                },
            )
        )
    ) as client:
        gateway = ResponsesGateway(ModelConfig(), Auth(), client)
        with pytest.raises(AgentError) as error:
            _ = [e async for e in gateway.stream(request())]
        assert error.value.code == AuthState.REAUTH_REQUIRED
        assert "secret" not in str(error.value)


async def test_unsupported_reasoning_value_is_a_request_error_without_protocol_masking():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                400,
                json={
                    "error": {
                        "code": "unsupported_value",
                        "param": "reasoning.effort",
                        "message": "secret provider diagnostic",
                    }
                },
            )
        )
    ) as client:
        gateway = ResponsesGateway(ModelConfig(), Auth(), client)
        with pytest.raises(AgentError) as failure:
            _ = [event async for event in gateway.stream(request())]
        assert failure.value.code == ErrorCode.MODEL_UNAVAILABLE
        assert "unsupported request parameter value" in failure.value.message
        assert not failure.value.retryable
        assert "secret" not in failure.value.message


@pytest.mark.parametrize("code", [None, "new_provider_error"])
async def test_http_error_open_codes_preserve_status_and_safe_parameter(code):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                400,
                json={
                    "error": {
                        "code": code,
                        "param": "input[12]",
                        "message": "secret request diagnostic",
                        "type": "invalid_request_error",
                    }
                },
            )
        )
    ) as client:
        gateway = ResponsesGateway(ModelConfig(), Auth(), client)
        with pytest.raises(AgentError) as failure:
            _ = [event async for event in gateway.stream(request())]
        assert failure.value.code == ErrorCode.MODEL_UNAVAILABLE
        assert "HTTP 400" in failure.value.message
        assert "param=input[12]" in failure.value.message
        assert "secret" not in failure.value.message
        assert not failure.value.retryable


async def test_unknown_input_status_parameter_is_actionable_without_protocol_masking():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                400,
                json={
                    "error": {
                        "code": "unknown_parameter",
                        "param": "input[19].status",
                        "message": "Unknown parameter: 'input[19].status'. secret",
                        "type": "invalid_request_error",
                    }
                },
            )
        )
    ) as client:
        gateway = ResponsesGateway(ModelConfig(), Auth(), client)
        with pytest.raises(AgentError) as failure:
            _ = [event async for event in gateway.stream(request())]
        assert failure.value.code == ErrorCode.MODEL_UNAVAILABLE
        assert "unknown request parameter" in failure.value.message
        assert "HTTP 400" in failure.value.message
        assert "code=unknown_parameter" in failure.value.message
        assert "param=input[19].status" in failure.value.message
        assert "secret" not in failure.value.message
        assert not failure.value.retryable


@pytest.mark.parametrize("code", [None, "new_provider_error"])
async def test_stream_error_open_codes_do_not_mask_provider_failure(code):
    event: EventFixture = {
        "type": ResponseEventType.ERROR,
        "code": code,
        "message": "secret stream diagnostic",
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=Fragments([event]))
        )
    ) as client:
        gateway = ResponsesGateway(ModelConfig(), Auth(), client)
        with pytest.raises(AgentError) as failure:
            _ = [event async for event in gateway.stream(request())]
        assert failure.value.code == ErrorCode.MODEL_UNAVAILABLE
        assert "secret" not in failure.value.message
        assert "HTTP" not in failure.value.message


@pytest.mark.parametrize("status", [401, 403, 429])
async def test_nullable_http_error_code_retains_status_classification(status):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                status, json={"error": {"code": None, "message": "secret", "param": "secret"}}
            )
        )
    ) as client:
        gateway = ResponsesGateway(ModelConfig(), Auth(), client)
        with pytest.raises(AgentError) as failure:
            _ = [event async for event in gateway.stream(request())]
        match status:
            case 401:
                expected = ErrorCode.REAUTH_REQUIRED
            case 403:
                expected = ErrorCode.MODEL_ACCESS_DENIED
            case 429:
                expected = ErrorCode.MODEL_QUOTA_EXHAUSTED
        assert failure.value.code == expected
        assert f"HTTP {status}" in failure.value.message
        assert "secret" not in failure.value.message


@pytest.mark.parametrize(
    "body", [b"secret html error", b'{"error": {}}', b'{"error": {"code": 1}}']
)
async def test_malformed_http_error_preserves_status_without_accepting_invalid_protocol(body):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(400, content=body))
    ) as client:
        gateway = ResponsesGateway(ModelConfig(), Auth(), client)
        with pytest.raises(AgentError) as failure:
            _ = [event async for event in gateway.stream(request())]
        assert failure.value.code == ErrorCode.MODEL_PROTOCOL
        assert "HTTP 400" in failure.value.message
        assert "secret" not in failure.value.message


async def test_stream_error_event_classifies_quota_without_leaking_message():
    event = {
        "type": ResponseEventType.ERROR,
        "code": ProviderErrorCode.USAGE_LIMIT,
        NativeItemType.MESSAGE: "secret",
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=Fragments([event])))
    ) as client:
        gateway = ResponsesGateway(ModelConfig(), Auth(), client)
        with pytest.raises(AgentError) as error:
            _ = [e async for e in gateway.stream(request())]
        assert error.value.code == "model_quota_exhausted"
        assert "secret" not in str(error.value)


@pytest.mark.parametrize(
    "error_code,via_sse",
    [
        (ProviderErrorCode.CONTEXT_LENGTH_EXCEEDED, False),
        (ProviderErrorCode.CONTEXT_WINDOW_EXCEEDED, True),
    ],
)
async def test_provider_overflow_reaches_runtime_compaction(tmp_path, error_code, via_sse):
    from agent_client.application.prompts import user_item
    from agent_client.application.runtime import AgentRuntime
    from agent_client.domain.configuration import AppConfig
    from agent_client.infrastructure.persistence.store import SessionStore

    config = AppConfig()
    config.runtime.max_model_steps = 1
    summary = {
        "goal": "continue",
        **{
            name: []
            for name in [
                "constraints",
                "decisions",
                "modified_files",
                "validation",
                "pending",
                "skills",
                "evidence",
            ]
        },
    }
    calls = []

    def completion(text, identity):
        return httpx.Response(
            200,
            stream=Fragments(
                [
                    {
                        "type": ResponseEventType.COMPLETED,
                        "response": {
                            "id": identity,
                            "output": [
                                {
                                    "type": NativeItemType.MESSAGE,
                                    "role": MessageRole.ASSISTANT,
                                    "content": [{"type": ContentType.OUTPUT_TEXT, "text": text}],
                                }
                            ],
                        },
                    }
                ]
            ),
        )

    def handler(req):
        calls.append(json.loads(req.content))
        if len(calls) == 1:
            if via_sse:
                return httpx.Response(
                    200,
                    stream=Fragments(
                        [
                            {
                                "type": ResponseEventType.FAILED,
                                "response": {ResponseEventType.ERROR: {"code": error_code}},
                            }
                        ]
                    ),
                )
            return httpx.Response(400, json={ResponseEventType.ERROR: {"code": error_code}})
        if len(calls) == 2:
            return completion(json.dumps(summary), "summary")
        return completion("done", "final")

    class Tools:
        async def project_output(self, session_id, result):
            return result.content.model_dump_json()

        def release_result(self, result):
            pass

        async def instructions(self, workspace):
            return ""

        def specs(self):
            return []

        async def cancel_session(self, session):
            return []

        def owned_commands(self, session):
            return set()

    async def quiet(event):
        pass

    store = await SessionStore(tmp_path / "home").open()
    try:
        session = await store.create_session(tmp_path)
        for text in ["old " * 1000, "middle", "current"]:
            await store.append(session, JournalEventType.USER_MESSAGE, user_item(text))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            gateway = ResponsesGateway(config.model, Auth(), client)
            runtime = AgentRuntime(config, store, gateway, Tools())
            result = await runtime.run(session, "continue", quiet)
            assert result.status == ModelResponseStatus.COMPLETED
            assert len(calls) == 3
            checkpoints = [
                r
                for r in await store.read(session)
                if r.type == JournalEventType.COMPACTION_COMMITTED
            ]
            assert (
                len(checkpoints) == 1
                and checkpoints[0].payload.reason == CompactionReason.PROVIDER_OVERFLOW
            )
    finally:
        await store.close()


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "unrecognized.event"},
        {"type": ResponseEventType.COMPLETED},
        {"type": ResponseEventType.ITEM_DONE, "output_index": 0},
        {"type": ResponseEventType.ERROR},
    ],
)
def test_invalid_event_discriminators_and_missing_payload_fail_fast(payload: EventFixture):
    with pytest.raises(ValidationError):
        ResponseEvent.model_validate(payload)


async def test_unknown_sse_event_interrupts_before_completion():
    events = [
        {"type": "unrecognized.event"},
        {"type": ResponseEventType.COMPLETED, "response": {"id": "r", "output": []}},
    ]

    def handler(req):
        return httpx.Response(200, stream=Fragments(events))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        gateway = ResponsesGateway(ModelConfig(), Auth(), client)
        with pytest.raises(AgentError) as failure:
            _ = [event async for event in gateway.stream(request())]
        assert failure.value.code == ErrorCode.MODEL_OUTCOME_UNKNOWN


@pytest.mark.parametrize("channel", list(ReasoningChannel))
@pytest.mark.parametrize("use_deltas,use_done", [(True, True), (False, True), (False, False)])
async def test_visible_reasoning_stream_completion_and_native_preservation(
    channel, use_deltas, use_done
):
    summary = channel == ReasoningChannel.SUMMARY
    delta_type = (
        ResponseEventType.REASONING_SUMMARY_DELTA if summary else ResponseEventType.REASONING_DELTA
    )
    done_type = (
        ResponseEventType.REASONING_SUMMARY_DONE if summary else ResponseEventType.REASONING_DONE
    )
    content_type = ContentType.SUMMARY_TEXT if summary else ContentType.REASONING_TEXT
    item: NativeFixture = {
        "id": "reason1",
        "type": NativeItemType.REASONING,
        "encrypted_content": "opaque-secret",
    }
    item["summary" if summary else "content"] = [{"type": content_type, "text": "Inspect source"}]
    identity: EventFixture = {"item_id": "reason1", "output_index": 0}
    identity["summary_index" if summary else "content_index"] = 0
    events: list[EventFixture] = []
    if use_deltas:
        events.extend(
            [
                {**identity, "type": delta_type, "delta": "Inspect "},
                {**identity, "type": delta_type, "delta": "source"},
            ]
        )
    if use_done:
        events.append({**identity, "type": done_type, "text": "Inspect source"})
    events.extend(
        [
            {"type": ResponseEventType.ITEM_DONE, "output_index": 0, "item": item},
            {"type": ResponseEventType.COMPLETED, "response": {"id": "r", "output": [item]}},
        ]
    )

    def handler(req):
        assert json.loads(req.content)["reasoning"]["summary"] == "auto"
        return httpx.Response(200, stream=Fragments(events))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        gateway = ResponsesGateway(ModelConfig(), Auth(), client)
        result = [event async for event in gateway.stream(request())]
    deltas = [event.reasoning for event in result if event.kind == ModelEventKind.REASONING_DELTA]
    assert "".join(block.text for block in deltas) == "Inspect source"
    assert all(block.channel == channel and block.item_id == "reason1" for block in deltas)
    assert all(event.text == "" for event in result)
    final = result[-1].response
    assert final.text == ""
    assert final.reasoning[0].text == "Inspect source"
    assert final.output[0].encrypted_content == "opaque-secret"
    assert "opaque-secret" not in "".join(block.text for block in final.reasoning)


async def test_encrypted_reasoning_without_visible_content_is_not_displayed():
    item: NativeFixture = {"type": NativeItemType.REASONING, "encrypted_content": "opaque"}
    events: list[EventFixture] = [
        {"type": ResponseEventType.COMPLETED, "response": {"id": "r", "output": [item]}}
    ]

    def handler(req):
        return httpx.Response(200, stream=Fragments(events))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        gateway = ResponsesGateway(ModelConfig(), Auth(), client)
        result = [event async for event in gateway.stream(request())]
    assert len(result) == 1
    assert result[0].response.reasoning == []


async def test_incomplete_response_preserves_diagnostics_without_executable_calls():
    from agent_client.domain.protocol import IncompleteReason

    events = [
        {
            "type": "response.incomplete",
            "response": {
                "id": "partial-1",
                "status": "incomplete",
                "output": [
                    {
                        "type": "message",
                        "id": "partial-message",
                        "role": "assistant",
                        "status": "incomplete",
                        "phase": "commentary",
                        "content": [{"type": "output_text", "text": "partial"}],
                    }
                ],
                "usage": {"input_tokens": 12, "output_tokens": 8},
                "incomplete_details": {"reason": "max_output_tokens"},
            },
        }
    ]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=Fragments(events)))
    ) as client:
        emitted = [
            event
            async for event in ResponsesGateway(ModelConfig(), Auth(), client).stream(request())
        ]
    response = emitted[-1].response
    assert response.status == ModelResponseStatus.INCOMPLETE
    assert response.calls == []
    assert response.text == "partial"
    assert response.usage.output_tokens == 8
    assert response.incomplete_reason == IncompleteReason.MAX_OUTPUT_TOKENS
