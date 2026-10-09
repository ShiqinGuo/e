import asyncio
from enum import StrEnum

import httpx
import pytest

from agent_client.application.runtime import AgentRuntime
from agent_client.application.tools import ToolService
from agent_client.domain.chat import (
    ChatChoice,
    ChatChunk,
    ChatDelta,
    ChatFinishReason,
    ChatFunctionDelta,
    ChatRequest,
    ChatRole,
    ChatToolDelta,
)
from agent_client.domain.configuration import AppConfig, ModelConfig
from agent_client.domain.enums import (
    AuthMode,
    ErrorCode,
    JournalEventType,
    ModelEventKind,
    NativeItemType,
    ProviderKind,
    RunStatus,
    RuntimeEventKind,
    StopReason,
)
from agent_client.domain.errors import AgentError
from agent_client.domain.events import RuntimeEvent
from agent_client.domain.models import ModelEvent, ModelResponse
from agent_client.domain.runtime import ModelCommitted, RunFinished, RunStarted
from agent_client.infrastructure.models.chat import ChatCompletionsGateway
from agent_client.infrastructure.persistence.store import SessionStore


def configuration() -> AppConfig:
    return AppConfig(
        model=ModelConfig(
            provider=ProviderKind.OPENAI_CHAT_COMPLETIONS,
            auth_mode=AuthMode.API_KEY,
            api_key_env="TEST_CHAT_RUNTIME_KEY",
            base_url="https://chat-provider.test/v1",
            model="test-model",
        )
    )


def stream_chunk(
    identifier: str, delta: ChatDelta, finish: ChatFinishReason | None = None
) -> ChatChunk:
    return ChatChunk(
        id=identifier, choices=[ChatChoice(index=0, delta=delta, finish_reason=finish)]
    )


def response_stream(chunks: list[ChatChunk]) -> str:
    return (
        "".join(f"data: {chunk.model_dump_json(exclude_none=True)}\n\n" for chunk in chunks)
        + "data: [DONE]\n\n"
    )


async def test_chat_tool_round_reasoning_history_and_reload_use_real_journal_and_projection(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("TEST_CHAT_RUNTIME_KEY", "offline-key")
    config = configuration()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    await asyncio.to_thread(
        (workspace / "task.txt").write_text, "Actual file contents", encoding="utf-8"
    )
    bodies: list[ChatRequest] = []

    def handle(request: httpx.Request):
        assert str(request.url) == "https://chat-provider.test/v1/chat/completions"
        bodies.append(ChatRequest.model_validate_json(request.content))
        if len(bodies) == 1:
            chunks = [
                stream_chunk("tool-round", ChatDelta(reasoning_content="Inspect file")),
                stream_chunk(
                    "tool-round",
                    ChatDelta(
                        tool_calls=[
                            ChatToolDelta(
                                index=0,
                                id="read-task",
                                function=ChatFunctionDelta(
                                    name="read_file", arguments='{"path":"task.txt"}'
                                ),
                            )
                        ]
                    ),
                    ChatFinishReason.TOOL_CALLS,
                ),
            ]
        else:
            chunks = [
                stream_chunk(
                    f"answer-{len(bodies)}",
                    ChatDelta(reasoning_content="Explain result", content="Answer"),
                    ChatFinishReason.STOP,
                )
            ]
        return httpx.Response(200, text=response_stream(chunks))

    observed: list[RuntimeEvent] = []

    async def emit(event: RuntimeEvent):
        observed.append(event)

    store = await SessionStore(tmp_path / "home").open()
    tools = ToolService(config, store)
    await tools.start()
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            gateway = ChatCompletionsGateway(config.model, client)
            runtime = AgentRuntime(config, store, gateway, tools)
            session = await store.create_session(workspace)
            result = await runtime.run(session, "Read task.txt", emit)
            assert result.status == RunStatus.COMPLETED
            assert result.text == "Answer"
            assert len(bodies) == 2
            assistant = next(message for message in bodies[1].messages if message.tool_calls)
            assert assistant.reasoning_content == "Inspect file"
            assert assistant.tool_calls[0].id == "read-task"
            tool = next(message for message in bodies[1].messages if message.role == ChatRole.TOOL)
            assert tool.tool_call_id == "read-task"
            assert "Actual file contents" in tool.content
            assert sum(event.kind == RuntimeEventKind.TOOL_DISPATCHING for event in observed) == 1
            records = await store.read(session)
            committed = [
                ModelCommitted.model_validate(record.payload)
                for record in records
                if record.type == JournalEventType.MODEL_RESPONSE_COMMITTED
            ]
            assert [item.response.reasoning[0].text for item in committed] == [
                "Inspect file",
                "Explain result",
            ]
            await store.close()
            await store.open()
            recovered = await store.recover(session)
            loaded = await runtime.context.load(session, recovered)
            assert any(item.type == NativeItemType.REASONING for item in loaded.items)
            result = await runtime.run(session, "Continue explaining", emit)
            assert result.status == RunStatus.COMPLETED
            assert len(bodies) == 3
            history = bodies[2].messages
            assert sum(message.role == ChatRole.TOOL for message in history) == 1
            assert [
                message.reasoning_content for message in history if message.reasoning_content
            ] == ["Inspect file", "Explain result"]
            assert sum(event.kind == RuntimeEventKind.TOOL_DISPATCHING for event in observed) == 1
    finally:
        await tools.close()
        await store.close()


class ForbiddenGateway:
    def __init__(self):
        self.requests = 0

    async def stream(self, request):
        self.requests += 1
        yield ModelEvent(
            kind=ModelEventKind.COMPLETED, response=ModelResponse(id="unexpected", output=[])
        )


class ForbiddenTools:
    async def instructions(self, workspace):
        raise AssertionError("Provider mismatch must fail before inspecting tools")

    def specs(self):
        raise AssertionError("Provider mismatch must fail before inspecting tools")


class RuntimeOperation(StrEnum):
    RUN = "run"
    COMPACT = "compact"


class BindingChange(StrEnum):
    PROVIDER = "provider"
    ENDPOINT = "endpoint"
    AUTHENTICATION = "authentication"
    LEGACY = "legacy"


@pytest.mark.parametrize("operation", list(RuntimeOperation))
@pytest.mark.parametrize("binding", list(BindingChange))
async def test_provider_binding_rejects_incompatible_history_before_model_and_tools(
    tmp_path, operation, binding
):
    config = configuration()
    previous = RunStarted(
        status=RunStatus.RUNNING,
        model="previous",
        prefix_revision="prefix",
        instructions="Help",
        tools=[],
        provider=config.model.provider,
        base_url=config.model.base_url,
        auth_mode=config.model.auth_mode,
    )
    match binding:
        case BindingChange.PROVIDER:
            previous.provider = ProviderKind.OPENAI_RESPONSES
        case BindingChange.ENDPOINT:
            previous.base_url = "https://other-provider.test/v1"
        case BindingChange.AUTHENTICATION:
            previous.auth_mode = AuthMode.CHATGPT
        case BindingChange.LEGACY:
            previous = RunStarted(
                status=RunStatus.RUNNING,
                model="previous",
                prefix_revision="prefix",
                instructions="Help",
                tools=[],
            )
    store = await SessionStore(tmp_path / "home").open()
    try:
        session = await store.create_session(tmp_path)
        if binding == BindingChange.LEGACY:
            previous = RunStarted.model_validate(
                previous.model_dump(mode="json", exclude={"provider", "base_url", "auth_mode"})
            )
            assert "provider" not in previous.model_fields_set
            assert previous.provider == ProviderKind.OPENAI_RESPONSES
        await store.append(session, JournalEventType.RUN_STARTED, previous, run_id="original")
        await store.append(
            session,
            JournalEventType.RUN_FINISHED,
            RunFinished(status=RunStatus.COMPLETED, stop_reason=StopReason.COMPLETED),
            run_id="original",
        )
        gateway = ForbiddenGateway()
        runtime = AgentRuntime(config, store, gateway, ForbiddenTools())

        async def emit(event):
            raise AssertionError("Provider mismatch must fail before emitting runtime work")

        with pytest.raises(AgentError) as failure:
            if operation == RuntimeOperation.RUN:
                await runtime.run(session, "Continue", emit)
            else:
                await runtime.compact(session, emit)
        assert failure.value.code == ErrorCode.INVALID_CONTEXT
        assert gateway.requests == 0
    finally:
        await store.close()


@pytest.mark.parametrize("operation", list(RuntimeOperation))
@pytest.mark.parametrize(
    "target",
    [
        ModelConfig(auth_mode=AuthMode.API_KEY),
        ModelConfig(auth_mode=AuthMode.API_KEY, base_url="https://other-provider.test/v1"),
        configuration().model,
    ],
)
async def test_unbound_legacy_history_cannot_be_sent_to_api_key_endpoints(
    tmp_path, operation, target
):
    store = await SessionStore(tmp_path / "home").open()
    try:
        session = await store.create_session(tmp_path)
        await store.append(
            session,
            JournalEventType.RUN_STARTED,
            RunStarted(
                status=RunStatus.RUNNING,
                model="original",
                prefix_revision="prefix",
                instructions="Help",
                tools=[],
            ),
            run_id="original",
        )
        await store.append(
            session,
            JournalEventType.RUN_FINISHED,
            RunFinished(status=RunStatus.COMPLETED, stop_reason=StopReason.COMPLETED),
            run_id="original",
        )
        gateway = ForbiddenGateway()
        runtime = AgentRuntime(AppConfig(model=target), store, gateway, ForbiddenTools())

        async def emit(event):
            raise AssertionError("Unbound history must fail before emitting runtime work")

        with pytest.raises(AgentError) as failure:
            match operation:
                case RuntimeOperation.RUN:
                    await runtime.run(session, "Continue", emit)
                case RuntimeOperation.COMPACT:
                    await runtime.compact(session, emit)
        assert failure.value.code == ErrorCode.INVALID_CONTEXT
        assert gateway.requests == 0
    finally:
        await store.close()


@pytest.mark.parametrize(
    "previous",
    [
        RunStarted(
            status=RunStatus.RUNNING,
            model="original",
            prefix_revision="prefix",
            instructions="Help",
            tools=[],
        ),
        RunStarted(
            status=RunStatus.RUNNING,
            model="original",
            prefix_revision="prefix",
            instructions="Help",
            tools=[],
            base_url="https://api.openai.com/v1",
        ),
        RunStarted(
            status=RunStatus.RUNNING,
            model="original",
            prefix_revision="prefix",
            instructions="Help",
            tools=[],
            auth_mode=AuthMode.CHATGPT,
        ),
    ],
)
async def test_legacy_history_retains_original_official_subscription_access(tmp_path, previous):
    store = await SessionStore(tmp_path / "home").open()
    try:
        session = await store.create_session(tmp_path)
        await store.append(session, JournalEventType.RUN_STARTED, previous, run_id="original")
        runtime = AgentRuntime(AppConfig(), store, ForbiddenGateway(), ForbiddenTools())
        runtime.validate_provider(await store.read(session))
    finally:
        await store.close()
