import asyncio

import pytest

from agent_client.application.context import ContextManager, validate_pairs
from agent_client.application.prompts import SUMMARY_REQUEST, user_item
from agent_client.application.runtime import AgentRuntime
from agent_client.application.tool_output import ToolOutputProjector
from agent_client.domain.configuration import AppConfig
from agent_client.domain.enums import (
    AuthMode,
    ChatReasoningMode,
    CompactionReason,
    ContextStrategy,
    ErrorCode,
    JournalEventType,
    MessageRole,
    ModelEventKind,
    ModelResponseStatus,
    NativeItemType,
    ProviderKind,
    ReasoningChannel,
    ReasoningEffort,
    RunStatus,
    RuntimeEventKind,
    StopReason,
    ToolExecutionState,
    ToolStatus,
)
from agent_client.domain.errors import AgentError
from agent_client.domain.events import RuntimeEvent
from agent_client.domain.models import (
    ApprovalRequest,
    Effect,
    ModelEvent,
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
    NativeMessage,
    TokenUsage,
)
from agent_client.domain.runtime import (
    CompactionCommitted,
    ContextGroupKind,
    ContextInput,
    ContextWindow,
    ContinuationAction,
    ConversationSummary,
    InputDisposition,
    ModelCommitted,
    PendingInput,
    RunFinished,
    RunStarted,
    ToolStateChange,
    UserMessage,
)
from agent_client.domain.tools import ReadFileArguments, ToolDispatchEvent, ToolOutputRange
from agent_client.domain.workspace import ProcessResult
from agent_client.infrastructure.persistence.store import SessionStore


class ScriptedModel:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    async def stream(self, request):
        self.requests.append(request)
        validate_pairs(ContextWindow(items=request.items))
        yield ModelEvent(kind=ModelEventKind.COMPLETED, response=next(self.responses))


class Tools:
    async def project_output(self, session_id, result):
        return result.content.model_dump_json()

    def __init__(self):
        self.executed = []
        self.dispatched = asyncio.Event()
        self.hang = False

    def specs(self):
        return [ToolSpec(name="read_file", description="read_file", parameters={"type": "object"})]

    async def instructions(self, workspace):
        return "project rules"

    async def cancel_session(self, session_id):
        return []

    def owned_commands(self, session_id):
        return set()

    def release_result(self, result):
        pass

    async def execute(self, call, context, emit, approve):
        await emit(
            RuntimeEvent(
                kind=RuntimeEventKind.TOOL_DISPATCHING,
                session_id=context.session_id,
                run_id=context.run_id,
                data=ToolDispatchEvent(call_id=call.id, name=call.name, effect=Effect.READ),
            )
        )
        self.executed.append(call.id)
        self.dispatched.set()
        if self.hang:
            await asyncio.Event().wait()
        await asyncio.sleep(0.02 if call.id == "a" else 0)
        return ToolResult(
            call_id=call.id,
            content=ToolOutputRange(
                content="result " + call.id, total_characters=len("result " + call.id)
            ),
        )


def tool_response(ids=("a", "b")):
    calls = [
        ToolCall(id=call_id, name="read_file", arguments=ReadFileArguments(path="task.txt"))
        for call_id in ids
    ]
    return ModelResponse(
        id="step",
        calls=calls,
        output=[
            NativeFunctionCall(
                type=NativeItemType.FUNCTION_CALL,
                call_id=call.id,
                name=call.name,
                arguments='{"path":"task.txt"}',
            )
            for call in calls
        ],
    )


def text_response(text="done"):
    return ModelResponse(
        id="final",
        text=text,
        output=[
            NativeMessage(
                type=NativeItemType.MESSAGE,
                role=MessageRole.ASSISTANT,
                content=[NativeContent(type=ContentType.OUTPUT_TEXT, text=text)],
            )
        ],
    )


async def quiet(event):
    pass


async def test_new_session_runs_without_loading_an_unrelated_invalid_journal(store, tmp_path):
    old_session = await store.create_session(tmp_path)
    old_journal = store.home / "sessions" / old_session / "rollout.jsonl"
    old_journal.write_bytes(b"invalid historical record\n")
    session = await store.create_session(tmp_path)
    model = ScriptedModel([text_response()])
    runtime = AgentRuntime(AppConfig(), store, model, Tools())

    usage = await runtime.context_usage(session)
    result = await runtime.run(session, "new task", quiet)

    assert usage.used_tokens is None
    assert result.status == RunStatus.COMPLETED
    assert len(model.requests) == 1
    assert old_journal.read_bytes() == b"invalid historical record\n"
    with pytest.raises(AgentError):
        await store.get_session(old_session)


async def test_reported_context_restores_last_input_and_resets_after_compaction(store, tmp_path):
    session = await store.create_session(tmp_path)
    runtime = AgentRuntime(AppConfig(), store, ScriptedModel([]), Tools())
    response = text_response()
    response.usage = TokenUsage(input_tokens=1200, output_tokens=400)
    await store.append(
        session, JournalEventType.MODEL_RESPONSE_COMMITTED, ModelCommitted(response=response)
    )
    assert (await runtime.context_usage(session)).used_tokens == 1200
    await store.append(session, JournalEventType.USER_MESSAGE, user_item("New input " * 100))
    assert (await runtime.context_usage(session)).used_tokens == 1200
    artifact = await store.put_artifact(session, ContextWindow().model_dump_json())
    await store.append(
        session,
        JournalEventType.COMPACTION_COMMITTED,
        CompactionCommitted(
            epoch=1,
            source_seq=3,
            artifact_id=artifact,
            strategy=ContextStrategy.SUMMARY,
            before_tokens=1200,
            after_tokens=0,
            reason=CompactionReason.MANUAL,
        ),
    )
    assert (await runtime.context_usage(session)).used_tokens is None


async def test_reasoning_events_are_separate_and_survive_session_reload(store, tmp_path):
    block = ReasoningBlock(
        item_id="reason1", index=0, channel=ReasoningChannel.SUMMARY, text="Check source"
    )
    response = text_response("Answer")
    response.reasoning = [block]

    class ReasoningModel:
        async def stream(self, request):
            yield ModelEvent(kind=ModelEventKind.REASONING_DELTA, reasoning=block)
            yield ModelEvent(kind=ModelEventKind.TEXT_DELTA, text="Answer")
            yield ModelEvent(kind=ModelEventKind.COMPLETED, response=response)

    events: list[RuntimeEvent] = []

    async def collect(event):
        events.append(event)

    session = await store.create_session(tmp_path)
    runtime = AgentRuntime(AppConfig(), store, ReasoningModel(), Tools())
    result = await runtime.run(session, "Inspect", collect)
    assert result.text == "Answer"
    reasoning_events = [event for event in events if event.kind == RuntimeEventKind.REASONING_DELTA]
    assert [ReasoningBlock.model_validate(event.data) for event in reasoning_events] == [block]
    completed = next((event for event in events if event.kind == RuntimeEventKind.MODEL_COMPLETED))
    assert completed.data.reasoning == [block]
    await store.close()
    await store.open()
    committed = next(
        (
            record
            for record in await store.read(session)
            if record.type == JournalEventType.MODEL_RESPONSE_COMMITTED
        )
    )
    assert ModelCommitted.model_validate(committed.payload).response.reasoning == [block]


@pytest.mark.parametrize(
    "status", [RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.PARTIAL, RunStatus.INTERRUPTED]
)
async def test_continuation_is_durable_idempotent_and_uses_new_run(store, tmp_path, status):
    session = await store.create_session(tmp_path)
    runtime = AgentRuntime(AppConfig(), store, ScriptedModel([text_response()]), Tools())
    await store.append(
        session,
        JournalEventType.RUN_STARTED,
        RunStarted(
            status=RunStatus.RUNNING,
            model="test",
            prefix_revision="prefix",
            instructions="rules",
            tools=[],
        ),
        run_id="original",
    )
    await store.append(
        session, JournalEventType.USER_MESSAGE, user_item("Original task"), run_id="original"
    )
    if status != RunStatus.INTERRUPTED:
        await store.append(
            session,
            JournalEventType.RUN_FINISHED,
            RunFinished(status=status, stop_reason=StopReason.MODEL_BUDGET),
            run_id="original",
        )
    first = await runtime.prepare_continuation(session)
    assert first.action == ContinuationAction.PREPARED
    assert first.inputs[0].continuation_of == "original"
    await store.close()
    await store.open()
    second = await runtime.prepare_continuation(session)
    assert second.action == ContinuationAction.QUEUED
    assert second.inputs == first.inputs
    item = first.inputs[0]
    result = await runtime.run(session, item.prompt, quiet, command_id=item.command_id)
    assert result.status == RunStatus.COMPLETED
    assert result.run_id != "original"
    assert any(
        (
            "Original task" in message.model_dump_json()
            for message in runtime.model.requests[0].items
        )
    )
    assert (await runtime.prepare_continuation(session)).action == ContinuationAction.NO_TASK


async def test_continuation_after_auth_failure_and_budget_partial(store, tmp_path):

    class MissingAuth:
        async def stream(self, request):
            raise AgentError(ErrorCode.REAUTH_REQUIRED, "Login required")
            yield

    session = await store.create_session(tmp_path)
    config = AppConfig()
    runtime = AgentRuntime(config, store, MissingAuth(), Tools())
    command = await runtime.enqueue(session, "Original task")
    failed = await runtime.run(session, "Original task", quiet, command_id=command)
    assert failed.status == RunStatus.FAILED
    runtime.model = ScriptedModel([tool_response(("a",)), text_response()])
    config.runtime.max_model_steps = 1
    plan = await runtime.prepare_continuation(session)
    item = plan.inputs[0]
    partial = await runtime.run(session, item.prompt, quiet, command_id=item.command_id)
    assert partial.status == RunStatus.PARTIAL
    assert runtime.tools.executed == ["a"]
    config.runtime.max_model_steps = 40
    item = (await runtime.prepare_continuation(session)).inputs[0]
    completed = await runtime.run(session, item.prompt, quiet, command_id=item.command_id)
    assert completed.status == RunStatus.COMPLETED
    assert runtime.tools.executed == ["a"]


async def test_continuation_preserves_queue_and_rejects_active_session(store, tmp_path):
    session = await store.create_session(tmp_path)
    runtime = AgentRuntime(AppConfig(), store, ScriptedModel([]), Tools())
    assert (await runtime.prepare_continuation(session)).action == ContinuationAction.NO_TASK
    identity = await runtime.enqueue(session, "Queued task")
    plan = await runtime.prepare_continuation(session)
    assert plan.action == ContinuationAction.QUEUED
    assert [item.command_id for item in plan.inputs] == [identity]
    async with store.session_lock(session):
        with pytest.raises(AgentError) as caught:
            await runtime.prepare_continuation(session)
        assert caught.value.code == ErrorCode.SESSION_BUSY


@pytest.fixture
async def store(tmp_path):
    value = SessionStore(tmp_path / "home")
    await value.open()
    yield value
    await value.close()


async def test_parallel_results_are_durable_and_model_ordered(store, tmp_path):
    session = await store.create_session(tmp_path)
    second_committed = asyncio.Event()

    class OrderedTools(Tools):
        async def execute(self, call, context, emit, approve):
            result = await super().execute(call, context, emit, approve)
            if call.id == "a":
                await second_committed.wait()
            return result

    class OrderedRuntime(AgentRuntime):
        async def _result(self, session_id, run_id, result):
            await super()._result(session_id, run_id, result)
            if result.call_id == "b":
                second_committed.set()

    tools = OrderedTools()
    model = ScriptedModel([tool_response(), text_response()])
    runtime = OrderedRuntime(AppConfig(), store, model, tools)
    result = await runtime.run(session, "inspect", quiet, command_id="command")
    assert result.status == RunStatus.COMPLETED
    records = await store.read(session)
    results = [
        record.payload.result.call_id
        for record in records
        if record.type == JournalEventType.TOOL_RESULT_COMMITTED
    ]
    assert results == ["b", "a"]
    outputs = [
        item.call_id for item in model.requests[1].items if isinstance(item, NativeFunctionOutput)
    ]
    assert outputs == ["a", "b"]
    assert (
        await runtime.run(session, "inspect", quiet, command_id="command")
    ).run_id == result.run_id
    assert len(model.requests) == 2


async def test_cancel_after_dispatch_commits_unknown_without_replay(store, tmp_path):
    session = await store.create_session(tmp_path)
    tools = Tools()
    tools.hang = True
    runtime = AgentRuntime(AppConfig(), store, ScriptedModel([tool_response(("a",))]), tools)
    task = asyncio.create_task(runtime.run(session, "inspect", quiet))
    await tools.dispatched.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    records = await store.read(session)
    assert records[-1].payload.status == RunStatus.CANCELLED
    result = next(
        (record for record in records if record.type == JournalEventType.TOOL_RESULT_COMMITTED)
    )
    assert result.payload.result.status == ToolStatus.UNKNOWN
    active = await ContextManager(AppConfig(), store).load(session, records)
    validate_pairs(active)
    assert tools.executed == ["a"]


async def test_incomplete_model_response_never_dispatches(store, tmp_path):
    session = await store.create_session(tmp_path)
    response = tool_response(("a",))
    response.status = ModelResponseStatus.INCOMPLETE
    tools = Tools()
    runtime = AgentRuntime(AppConfig(), store, ScriptedModel([response]), tools)
    result = await runtime.run(session, "inspect", quiet)
    assert result.status == RunStatus.PARTIAL
    assert not tools.executed
    assert not any(
        (
            record.type == JournalEventType.MODEL_RESPONSE_COMMITTED
            for record in await store.read(session)
        )
    )


async def test_compaction_checkpoint_keeps_current_request_and_suffix(store, tmp_path):
    session = await store.create_session(tmp_path)
    for text in ["old " * 1000, "middle", "current"]:
        await store.append(session, JournalEventType.USER_MESSAGE, user_item(text))
        await store.append(
            session,
            JournalEventType.MODEL_RESPONSE_COMMITTED,
            ModelCommitted(response=text_response()),
        )
    summary = ConversationSummary(text="current")
    model = ScriptedModel([text_response(summary.text)])
    runtime = AgentRuntime(AppConfig(), store, model, Tools())
    await runtime.compact(session, quiet)
    await store.append(session, JournalEventType.USER_MESSAGE, user_item("suffix"))
    active = await runtime.context.load(session, await store.read(session))
    assert active.epoch == 1
    assert "current" in active.model_dump_json()
    assert "suffix" in active.model_dump_json()
    assert "old old" not in active.model_dump_json()
    assert model.requests[0].tools == []
    assert model.requests[0].reasoning_effort == ReasoningEffort.LOW
    assert model.requests[0].cache_key == "summary-v3"
    assert model.requests[0].items[-2] == user_item("current").item
    assert model.requests[0].items[-1] == user_item(SUMMARY_REQUEST).item
    assert any(
        isinstance(item, NativeMessage) and item.content == user_item("current").item.content
        for item in active.items
    )
    assert not any(
        isinstance(item, NativeMessage) and item.content == user_item(SUMMARY_REQUEST).item.content
        for item in active.items
    )


async def test_compaction_preserves_unresolved_native_call_pairs_and_guard(store, tmp_path):
    session = await store.create_session(tmp_path)
    await store.append(session, JournalEventType.USER_MESSAGE, user_item("old " * 1000))
    await store.append(
        session, JournalEventType.MODEL_RESPONSE_COMMITTED, ModelCommitted(response=text_response())
    )
    await store.append(session, JournalEventType.USER_MESSAGE, user_item("perform action"))
    await store.append(
        session,
        JournalEventType.MODEL_RESPONSE_COMMITTED,
        ModelCommitted(response=tool_response(("unknown",))),
    )
    runtime = AgentRuntime(
        AppConfig(),
        store,
        ScriptedModel([text_response("Earlier background"), text_response("We can still discuss")]),
        Tools(),
    )
    await runtime._result(
        session,
        "old",
        ToolResult(
            call_id="unknown",
            content=ToolOutputRange(content="Outcome unverified", total_characters=18),
            status=ToolStatus.UNKNOWN,
            is_error=True,
        ),
    )
    await store.append(session, JournalEventType.USER_MESSAGE, user_item("current discussion"))
    await runtime.compact(session, quiet)
    records = await store.read(session)
    active = await runtime.context.load(session, records)
    validate_pairs(active)
    assert active.epoch == 1
    assert any(
        isinstance(item, NativeFunctionCall) and item.call_id == "unknown" for item in active.items
    )
    assert any(
        isinstance(item, NativeFunctionOutput) and item.call_id == "unknown"
        for item in active.items
    )
    assert runtime._unresolved_unknown(records) == {"unknown"}
    assert (await runtime.run(session, "continue discussion", quiet)).status == RunStatus.COMPLETED


async def test_crash_after_dispatch_is_recovered_without_model_or_tool_replay(store, tmp_path):
    session = await store.create_session(tmp_path)
    response = tool_response(("a",))
    await store.append(
        session,
        JournalEventType.RUN_STARTED,
        RunStarted(
            status=RunStatus.RUNNING,
            model="test",
            prefix_revision="test",
            instructions="rules",
            tools=[],
        ),
        run_id="old",
    )
    await store.append(session, JournalEventType.USER_MESSAGE, user_item("inspect"), run_id="old")
    await store.append(
        session,
        JournalEventType.MODEL_RESPONSE_COMMITTED,
        ModelCommitted(response=response),
        run_id="old",
    )
    await store.append(
        session,
        JournalEventType.TOOL_CALL_STATE,
        ToolStateChange(call_id="a", state=ToolExecutionState.DISPATCHING),
        run_id="old",
    )
    tools = Tools()
    model = ScriptedModel(
        [
            text_response("The interrupted outcome remains unverified"),
            text_response("We can continue discussing it"),
        ]
    )
    runtime = AgentRuntime(AppConfig(), store, model, tools)
    result = await runtime.run(session, "continue", quiet)
    assert result.stop_reason == StopReason.COMPLETED
    assert not tools.executed and len(model.requests) == 1
    active = await runtime.context.load(session, await store.read(session))
    validate_pairs(active)
    output = next((item for item in active.items if isinstance(item, NativeFunctionOutput)))
    assert ToolStatus.UNKNOWN in output.output
    again = await runtime.run(session, "continue again", quiet)
    assert again.stop_reason == StopReason.COMPLETED
    plan = await runtime.prepare_continuation(session)
    assert plan.action == ContinuationAction.NO_TASK
    assert plan.unresolved_call_ids == ["a"]
    assert runtime._unresolved_unknown(await store.read(session)) == {"a"}
    assert plan.inputs == []
    await runtime.resolve_unknown(
        session, "a", ToolStatus.FAILED, "User inspected the workspace and chose not to retry"
    )
    model.responses = iter([text_response("resolved")])
    completed = await runtime.run(session, "continue after verification", quiet)
    assert completed.status == RunStatus.COMPLETED
    assert tools.executed == []


async def test_queue_survives_reload_and_consumes_command_once(store, tmp_path):
    session = await store.create_session(tmp_path)
    runtime = AgentRuntime(AppConfig(), store, ScriptedModel([text_response()]), Tools())
    command_id = await runtime.enqueue(session, "queued", "queue-id")
    await runtime.enqueue(session, "queued", "queue-id")
    other = AgentRuntime(AppConfig(), store, runtime.model, runtime.tools)
    assert await other.pending_inputs(session) == [
        PendingInput(command_id="queue-id", prompt="queued")
    ]
    result = await other.run(session, "queued", quiet, command_id=command_id)
    assert result.status == RunStatus.COMPLETED
    assert await other.pending_inputs(session) == []
    assert (
        await other.run(session, "queued", quiet, command_id=command_id)
    ).run_id == result.run_id


async def test_background_process_prevents_completed_answer(store, tmp_path):
    session = await store.create_session(tmp_path)

    class BackgroundTools(Tools):
        async def execute(self, call, context, emit, approve):
            return ToolResult(
                call_id=call.id,
                content=ProcessResult(running=True, process_handle="owned").model_dump(mode="json"),
            )

    runtime = AgentRuntime(
        AppConfig(),
        store,
        ScriptedModel([tool_response(("a",)), text_response()]),
        BackgroundTools(),
    )
    result = await runtime.run(session, "launch", quiet)
    assert (
        result.status == RunStatus.PARTIAL
        and result.stop_reason == StopReason.BACKGROUND_PROCESS_RUNNING
    )
    assert "owned" in result.text


async def test_invalid_summary_preserves_old_window(store, tmp_path):
    session = await store.create_session(tmp_path)
    for text in ["old " * 1000, "middle", "current"]:
        await store.append(session, JournalEventType.USER_MESSAGE, user_item(text))
    runtime = AgentRuntime(AppConfig(), store, ScriptedModel([text_response("   ")]), Tools())
    before = await runtime.context.load(session, await store.read(session))
    with pytest.raises(AgentError, match="Summary must contain nonempty text"):
        await runtime.compact(session, quiet)
    after = await runtime.context.load(session, await store.read(session))
    assert before.items == after.items and after.epoch == 0
    assert not any(
        (
            record.type == JournalEventType.COMPACTION_COMMITTED
            for record in await store.read(session)
        )
    )


async def test_expired_background_handle_requires_reconciliation(store, tmp_path):
    session = await store.create_session(tmp_path)
    runtime = AgentRuntime(
        AppConfig(),
        store,
        ScriptedModel([text_response("We can inspect the expired process")]),
        Tools(),
    )
    await store.append(session, JournalEventType.USER_MESSAGE, user_item("launch"))
    await store.append(
        session,
        JournalEventType.MODEL_RESPONSE_COMMITTED,
        ModelCommitted(response=tool_response(("a",))),
    )
    await runtime._result(
        session,
        "old",
        ToolResult(
            call_id="a",
            content=ProcessResult(running=True, process_handle="expired").model_dump(mode="json"),
        ),
    )
    result = await runtime.run(session, "continue", quiet)
    assert result.stop_reason == StopReason.COMPLETED
    assert len(runtime.model.requests) == 1 and not runtime.tools.executed
    assert await runtime._running_processes(session) == set()
    await runtime.resolve_unknown(
        session, "a", ToolStatus.FAILED, "Process exited; no retry authorized"
    )
    runtime.model.responses = iter([text_response()])
    assert (await runtime.run(session, "report", quiet)).status == RunStatus.COMPLETED


async def test_native_response_group_is_preserved_before_ordered_results(store, tmp_path):
    session = await store.create_session(tmp_path)
    response = tool_response()
    response.output.extend(text_response("trailing native message").output)
    model = ScriptedModel([response, text_response()])
    runtime = AgentRuntime(AppConfig(), store, model, Tools())
    assert (await runtime.run(session, "inspect", quiet)).status == RunStatus.COMPLETED
    items = model.requests[1].items
    assert items[1:4] == response.output
    assert [item.call_id for item in items[4:]] == ["a", "b"]


@pytest.mark.parametrize("second_overflow", [False, True])
async def test_provider_overflow_compacts_once_then_retries_same_step(
    store, tmp_path, second_overflow
):
    session = await store.create_session(tmp_path)
    for text in ["old " * 1000, "middle", "current"]:
        await store.append(session, JournalEventType.USER_MESSAGE, user_item(text))
    summary = ConversationSummary(text="current")

    class OverflowModel(ScriptedModel):
        async def stream(self, request):
            self.requests.append(request)
            value = next(self.responses)
            if isinstance(value, AgentError):
                raise value
            yield ModelEvent(kind=ModelEventKind.COMPLETED, response=value)

    final = AgentError(ErrorCode.CONTEXT_OVERFLOW, "again") if second_overflow else text_response()
    model = OverflowModel(
        [
            AgentError(ErrorCode.CONTEXT_OVERFLOW, "overflow"),
            text_response(summary.text),
            final,
        ]
    )
    config = AppConfig()
    config.runtime.max_model_steps = 1
    runtime = AgentRuntime(config, store, model, Tools())
    assert (await runtime.run(session, "continue", quiet)).status == (
        RunStatus.FAILED if second_overflow else RunStatus.COMPLETED
    )
    assert len(model.requests) == 3
    checkpoints = [
        record
        for record in await store.read(session)
        if record.type == JournalEventType.COMPACTION_COMMITTED
    ]
    assert (
        len(checkpoints) == 1
        and checkpoints[0].payload.reason == CompactionReason.PROVIDER_OVERFLOW
    )


async def test_approval_wait_does_not_consume_execution_deadline(store, tmp_path):
    session = await store.create_session(tmp_path)

    class ApprovalTools(Tools):
        async def execute(self, call, context, emit, approve):
            accepted = await approve(
                ApprovalRequest(
                    id="approval",
                    call_id=call.id,
                    tool=call.name,
                    description="test",
                    arguments=call.arguments,
                )
            )
            assert accepted
            return await super().execute(call, context, emit, approve)

    async def approval(request):
        await asyncio.sleep(1.2)
        return True

    config = AppConfig()
    config.runtime.deadline_seconds = 1
    runtime = AgentRuntime(
        config, store, ScriptedModel([tool_response(("a",)), text_response()]), ApprovalTools()
    )
    events = []

    async def capture(event):
        events.append(event)

    assert (await runtime.run(session, "approve", capture, approval)).status == RunStatus.COMPLETED
    completed = [event for event in events if event.kind == RuntimeEventKind.MODEL_COMPLETED]
    assert len(completed) == 2
    assert completed[0].data.duration_seconds >= 0
    assert completed[0].data.cached_input_tokens is None
    assert completed[0].data.context_hash


async def test_single_user_long_tool_chain_compacts_complete_steps_and_resumes(store, tmp_path):
    session = await store.create_session(tmp_path)
    prompt = "Inspect all eight modules and preserve every verification result"
    summary = ConversationSummary(text="Preserve task goal and verified progress")

    class LongChainModel(ScriptedModel):
        def __init__(self):
            super().__init__([])
            self.steps = 0
            self.compactions = 0

        async def stream(self, request):
            self.requests.append(request)
            validate_pairs(ContextWindow(items=request.items))
            if not request.tools:
                self.compactions += 1
                assert prompt in request.items[-2].content[0].text
                assert request.items[-1] == user_item(SUMMARY_REQUEST).item
                response = text_response(summary.text)
            elif self.steps < 8:
                self.steps += 1
                response = tool_response((f"step-{self.steps}",))
                response.output.extend(
                    text_response(f"Native trailing message {self.steps}").output
                )
            else:
                response = text_response("All modules inspected")
            yield ModelEvent(kind=ModelEventKind.COMPLETED, response=response)

    class LargeTools(Tools):
        async def execute(self, call, context, emit, approve):
            result = await super().execute(call, context, emit, approve)
            result.content = ToolOutputRange(
                content="module evidence " * 250, total_characters=len("module evidence " * 250)
            )
            return result

    config = AppConfig()
    config.model.max_output_tokens = 1024
    config.model.context_window = 8192
    config.context.summary_max_output_tokens = 512
    config.context.target_ratio = 0.8
    tools = LargeTools()
    model = LongChainModel()
    runtime = AgentRuntime(config, store, model, tools)
    result = await runtime.run(session, prompt, quiet)
    assert result.status == RunStatus.COMPLETED
    assert model.compactions > 0
    assert tools.executed == [f"step-{index}" for index in range(1, 9)]
    records = await store.read(session)
    assert len([record for record in records if record.type == JournalEventType.USER_MESSAGE]) == 1
    active = await runtime.context.load(session, records)
    assert active.epoch == model.compactions
    assert (
        sum(
            item.model_dump_json() == user_item(prompt).item.model_dump_json()
            for item in active.items
        )
        == 1
    )
    validate_pairs(active)
    for group in active.groups:
        if group.kind == ContextGroupKind.MODEL_STEP:
            assert group.complete
            validate_pairs(ContextWindow(items=active.items[group.start : group.end]))
    normal_requests = [request for request in model.requests if request.tools]
    assert len({request.cache_key for request in normal_requests}) == 1
    assert len({request.instructions for request in normal_requests}) == 1
    reopened = SessionStore(store.home)
    await reopened.open()
    try:
        resumed = AgentRuntime(config, reopened, ScriptedModel([text_response("Resumed")]), tools)
        loaded = await resumed.context.load(session, await reopened.recover(session))
        assert loaded.epoch == active.epoch and loaded.groups == active.groups
        assert (
            await resumed.run(session, "Report the final evidence", quiet)
        ).status == RunStatus.COMPLETED
        assert tools.executed == [f"step-{index}" for index in range(1, 9)]
    finally:
        await reopened.close()


async def test_repeated_runtime_cancellation_waits_for_cleanup_before_unlock(store, tmp_path):
    session = await store.create_session(tmp_path)
    cleanup_entered = asyncio.Event()
    release_cleanup = asyncio.Event()

    class CleanupTools(Tools):
        async def cancel_session(self, session_id):
            cleanup_entered.set()
            await release_cleanup.wait()
            return []

    tools = CleanupTools()
    tools.hang = True
    runtime = AgentRuntime(AppConfig(), store, ScriptedModel([tool_response(("a",))]), tools)
    task = asyncio.create_task(runtime.run(session, "Inspect", quiet))
    await tools.dispatched.wait()
    task.cancel()
    await cleanup_entered.wait()
    second = SessionStore(store.home)
    await second.open()
    try:
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0)
        assert not task.done()
        with pytest.raises(AgentError) as error:
            async with second.session_lock(session):
                pytest.fail("Session execution lock was released during cleanup")
        assert error.value.code == ErrorCode.SESSION_BUSY
    finally:
        release_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await second.close()
    records = await store.read(session)
    assert records[-1].type == JournalEventType.RUN_FINISHED
    assert records[-1].payload.status == RunStatus.CANCELLED
    validate_pairs(await runtime.context.load(session, records))
    recovery_model = ScriptedModel([text_response("Continue inspecting")])
    recovered = AgentRuntime(AppConfig(), store, recovery_model, tools)
    assert (await recovered.run(session, "Continue", quiet)).stop_reason == StopReason.COMPLETED
    assert len(recovery_model.requests) == 1
    assert tools.executed == ["a"]


async def test_missing_checkpoint_artifact_before_commit_keeps_old_window(
    store, tmp_path, monkeypatch
):
    session = await store.create_session(tmp_path)
    for text in ["old " * 1000, "middle", "current"]:
        await store.append(session, JournalEventType.USER_MESSAGE, user_item(text))
    summary = ConversationSummary(text="current")
    runtime = AgentRuntime(
        AppConfig(), store, ScriptedModel([text_response(summary.text)]), Tools()
    )
    before = await runtime.context.load(session, await store.read(session))

    async def missing_artifact(session_id, content):
        return "0" * 64

    monkeypatch.setattr(store, "put_artifact", missing_artifact)
    with pytest.raises(AgentError) as error:
        await runtime.compact(session, quiet)
    assert error.value.code == ErrorCode.ARTIFACT_MISSING
    records = await store.read(session)
    assert not any((record.type == JournalEventType.COMPACTION_COMMITTED for record in records))
    after = await runtime.context.load(session, records)
    assert after == before and after.epoch == 0


async def test_failed_checkpoint_write_keeps_old_window(store, tmp_path, monkeypatch):
    session = await store.create_session(tmp_path)
    for text in ["old " * 1000, "middle", "current"]:
        await store.append(session, JournalEventType.USER_MESSAGE, user_item(text))
    summary = ConversationSummary(text="current")
    runtime = AgentRuntime(
        AppConfig(), store, ScriptedModel([text_response(summary.text)]), Tools()
    )
    before = await runtime.context.load(session, await store.read(session))

    async def failed_artifact(session_id, content):
        raise OSError("Simulated checkpoint write failure")

    monkeypatch.setattr(store, "put_artifact", failed_artifact)
    with pytest.raises(OSError, match="checkpoint write"):
        await runtime.compact(session, quiet)
    records = await store.read(session)
    assert not any((record.type == JournalEventType.COMPACTION_COMMITTED for record in records))
    assert await runtime.context.load(session, records) == before


async def test_missing_committed_checkpoint_stops_recovery_before_model_call(
    store, tmp_path, monkeypatch
):
    session = await store.create_session(tmp_path)
    for text in ["old " * 1000, "middle", "current"]:
        await store.append(session, JournalEventType.USER_MESSAGE, user_item(text))
    summary = ConversationSummary(text="current")
    runtime = AgentRuntime(
        AppConfig(), store, ScriptedModel([text_response(summary.text)]), Tools()
    )
    await runtime.compact(session, quiet)
    records = await store.read(session)
    active = await runtime.context.load(session, records)
    assert active.epoch == 1

    async def missing_reference(session_id, artifact_id):
        raise AgentError(ErrorCode.ARTIFACT_MISSING, "Referenced checkpoint is unavailable")

    monkeypatch.setattr(store, "read_artifact", missing_reference)
    model = ScriptedModel([])
    recovered = AgentRuntime(AppConfig(), store, model, Tools())
    with pytest.raises(AgentError) as error:
        await recovered.run(session, "Continue", quiet)
    assert error.value.code == ErrorCode.ARTIFACT_MISSING
    assert not model.requests
    assert await store.read(session) == records


async def test_single_unsplittable_summary_group_is_rejected_without_calling_model(store, tmp_path):
    session = await store.create_session(tmp_path)
    await store.append(session, JournalEventType.USER_MESSAGE, user_item("old " * 10000))
    await store.append(session, JournalEventType.USER_MESSAGE, user_item("current"))
    config = AppConfig()
    config.model.max_output_tokens = 1024
    config.model.context_window = 8192
    config.context.summary_max_output_tokens = 512
    model = ScriptedModel([])
    runtime = AgentRuntime(config, store, model, Tools())
    before = await runtime.context.load(session, await store.read(session))
    with pytest.raises(AgentError) as error:
        await runtime.compact(session, quiet)
    assert error.value.code == ErrorCode.CONTEXT_BUDGET
    assert not model.requests
    assert await runtime.context.load(session, await store.read(session)) == before


async def test_single_oversized_complete_batch_stops_without_cutting_history(store, tmp_path):
    session = await store.create_session(tmp_path)

    class HugeTools(Tools):
        async def project_output(self, session_id, result):
            return await ToolOutputProjector(config.context, store).project(session_id, result)

        async def execute(self, call, context, emit, approve):
            result = await super().execute(call, context, emit, approve)
            result.content = ToolOutputRange(
                content="evidence " * 10000, total_characters=len("evidence " * 10000)
            )
            return result

    config = AppConfig()
    config.model.max_output_tokens = 1024
    config.model.context_window = 8192
    config.context.summary_max_output_tokens = 512
    model = ScriptedModel([tool_response(("huge",))])
    tools = HugeTools()
    runtime = AgentRuntime(config, store, model, tools)
    result = await runtime.run(session, "Inspect one large module", quiet)
    assert result.stop_reason == ErrorCode.CONTEXT_BUDGET
    assert len(model.requests) == 1 and tools.executed == ["huge"]
    records = await store.read(session)
    assert not any((record.type == JournalEventType.COMPACTION_COMMITTED for record in records))
    active = await runtime.context.load(session, records)
    validate_pairs(active)
    assert active.epoch == 0
    committed = next(
        (record for record in records if record.type == JournalEventType.TOOL_RESULT_COMMITTED)
    )
    result_payload = committed.payload.result
    assert await store.read_artifact(session, result_payload.artifact_id) == ToolOutputRange(
        content="evidence " * 10000, total_characters=len("evidence " * 10000)
    ).model_dump_json(exclude_none=True)


async def test_pending_tool_batch_prevents_compaction_without_summary_call(store, tmp_path):
    session = await store.create_session(tmp_path)
    await store.append(session, JournalEventType.USER_MESSAGE, user_item("Current task"))
    await store.append(
        session,
        JournalEventType.MODEL_RESPONSE_COMMITTED,
        ModelCommitted(response=tool_response(("pending",))),
    )
    model = ScriptedModel([])
    runtime = AgentRuntime(AppConfig(), store, model, Tools())
    before = await store.read(session)
    with pytest.raises(AgentError) as error:
        await runtime.compact(session, quiet)
    assert error.value.code == ErrorCode.INVALID_CONTEXT
    assert not model.requests
    assert await store.read(session) == before


async def test_cancellation_during_initial_journal_setup_records_terminal_run(
    store, tmp_path, monkeypatch
):
    session = await store.create_session(tmp_path)
    started = asyncio.Event()
    original = store.append

    async def delayed_append(session_id, event_type, payload, run_id=None, event_id=None):
        record = await original(session_id, event_type, payload, run_id, event_id)
        if event_type == JournalEventType.RUN_STARTED:
            started.set()
            await asyncio.Event().wait()
        return record

    monkeypatch.setattr(store, "append", delayed_append)
    model = ScriptedModel([])
    runtime = AgentRuntime(AppConfig(), store, model, Tools())
    task = asyncio.create_task(runtime.run(session, "Inspect", quiet))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    records = await store.read(session)
    assert records[-1].type == JournalEventType.RUN_FINISHED
    assert records[-1].payload.status == RunStatus.CANCELLED
    assert not model.requests


async def test_incomplete_summary_preserves_context_and_records_partial_response(store, tmp_path):
    session = await store.create_session(tmp_path)
    for text in ["Old evidence " * 1000, "Earlier request", "Current task"]:
        await store.append(session, JournalEventType.USER_MESSAGE, user_item(text))
    config = AppConfig()
    config.model.auth_mode = AuthMode.API_KEY
    config.model.provider = ProviderKind.OPENAI_CHAT_COMPLETIONS
    config.model.base_url = "https://api.deepseek.com"
    config.model.reasoning_effort = ReasoningEffort.HIGH
    config.model.max_output_tokens = 512
    config.model.chat_reasoning = ChatReasoningMode.ENABLED
    config.model.reasoning_levels = [
        ReasoningEffort.NONE,
        ReasoningEffort.HIGH,
        ReasoningEffort.MAX,
    ]
    incomplete = text_response("Partial summary")
    incomplete.status = ModelResponseStatus.INCOMPLETE
    model = ScriptedModel([incomplete])
    runtime = AgentRuntime(config, store, model, Tools())
    before = await runtime.context.load(session, await store.read(session))
    with pytest.raises(AgentError) as failure:
        await runtime.compact(session, quiet)
    assert failure.value.code == ErrorCode.MODEL_INCOMPLETE
    records = await store.read(session)
    after = await runtime.context.load(session, records)
    assert before.model_dump_json() == after.model_dump_json()
    assert not any(record.type == JournalEventType.COMPACTION_COMMITTED for record in records)
    recorded = [
        record for record in records if record.type == JournalEventType.MODEL_RESPONSE_INCOMPLETE
    ]
    assert len(recorded) == 1
    assert recorded[0].payload.response == incomplete
    assert model.requests[0].reasoning_effort == ReasoningEffort.NONE
    assert model.requests[0].max_output_tokens == config.context.summary_max_output_tokens
    assert not model.requests[0].tools
    assert not runtime.tools.executed


async def test_steer_updates_same_run_after_tool_batch_and_preserves_explicit_queue(
    store, tmp_path
):
    session = await store.create_session(tmp_path)
    model = ScriptedModel([tool_response(), text_response("adjusted")])
    tools = Tools()
    runtime = AgentRuntime(AppConfig(), store, model, tools)
    submissions = []
    consumed_events = []

    async def emit(event):
        match event.kind:
            case RuntimeEventKind.TOOL_DISPATCHING:
                if event.data.call_id == "a":
                    submissions.append(
                        await runtime.steer(session, "Change direction", event.run_id)
                    )
                    await runtime.enqueue(session, "Later task", "later")
            case RuntimeEventKind.INPUT_STEERED:
                consumed_events.append(event)

    result = await runtime.run(session, "Original task", emit)
    assert submissions[0].disposition == InputDisposition.STEERED
    assert submissions[0].run_id == result.run_id
    assert result.text == "adjusted"
    assert len(model.requests) == 2
    assert sorted(tools.executed) == ["a", "b"]
    assert model.requests[1].items[-1] == user_item("Change direction").item
    records = await store.read(session)
    user_records = [record for record in records if record.type == JournalEventType.USER_MESSAGE]
    assert len(user_records) == 2
    assert user_records[-1].run_id == result.run_id
    assert isinstance(user_records[-1].payload, UserMessage)
    assert user_records[-1].payload.command_id == submissions[0].command_id
    assert len(consumed_events) == 1
    assert [item.command_id for item in await runtime.pending_inputs(session)] == ["later"]


async def test_steer_at_final_response_continues_before_completion_and_late_steer_queues(
    store, tmp_path
):
    session = await store.create_session(tmp_path)
    model = ScriptedModel([text_response("first"), text_response("revised")])
    runtime = AgentRuntime(AppConfig(), store, model, Tools())
    submissions = []

    async def emit(event):
        if event.kind == RuntimeEventKind.MODEL_COMPLETED and not submissions:
            submissions.append(await runtime.steer(session, "Revise answer", event.run_id))

    result = await runtime.run(session, "Original", emit)
    assert result.text == "revised"
    assert len(model.requests) == 2
    assert await runtime.pending_inputs(session) == []
    late = await runtime.steer(session, "Next", result.run_id)
    assert late.disposition == InputDisposition.QUEUED
    assert late.run_id is None
    assert [item.command_id for item in await runtime.pending_inputs(session)] == [late.command_id]


async def test_cancelled_steer_remains_durable_without_replaying_tools(store, tmp_path):
    session = await store.create_session(tmp_path)
    tools = Tools()
    tools.hang = True
    runtime = AgentRuntime(AppConfig(), store, ScriptedModel([tool_response()]), tools)
    submissions = []

    async def emit(event):
        if event.kind == RuntimeEventKind.MODEL_REQUEST_STARTED and not submissions:
            submissions.append(await runtime.steer(session, "Keep this input", event.run_id))

    task = asyncio.create_task(runtime.run(session, "Original", emit))
    await tools.dispatched.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    pending = await runtime.pending_inputs(session)
    assert len(pending) == 1
    assert pending[0].command_id == submissions[0].command_id
    restarted = AgentRuntime(AppConfig(), store, ScriptedModel([]), Tools())
    assert await restarted.pending_inputs(session) == pending
    assert tools.executed
    assert len(tools.executed) == len(set(tools.executed))
    assert set(tools.executed) <= {"a", "b"}


async def populate_large_compaction_history(store, session):
    for index in range(8):
        await store.append(
            session, JournalEventType.USER_MESSAGE, user_item(f"Evidence-{index} " + "x" * 3000)
        )
    await store.append(session, JournalEventType.USER_MESSAGE, user_item("Current task"))


def small_compaction_config():
    config = AppConfig()
    config.model.max_output_tokens = 1024
    config.model.context_window = 8192
    config.context.summary_max_output_tokens = 512
    return config


async def test_oversized_summary_is_batched_without_dropping_source_groups(store, tmp_path):
    session = await store.create_session(tmp_path)
    await populate_large_compaction_history(store, session)
    model = ScriptedModel([text_response("Merged verified evidence")] * 8)
    runtime = AgentRuntime(small_compaction_config(), store, model, Tools())
    await runtime.compact(session, quiet)
    assert len(model.requests) > 1
    for index in range(8):
        assert (
            sum(
                f"Evidence-{index} " in item.model_dump_json()
                for request in model.requests
                for item in request.items
            )
            == 1
        )
    for request in model.requests:
        assert request.items[-2] == user_item("Current task").item
        validate_pairs(ContextWindow(items=request.items))
        assert (
            runtime.context.tokens(
                ContextInput(
                    instructions=request.instructions, tools=request.tools, items=request.items
                )
            )
            <= runtime.config.model.context_window
        )
    assert "Merged verified evidence" in model.requests[1].items[0].model_dump_json()
    active = await runtime.context.load(session, await store.read(session))
    assert active.epoch == 1
    assert active.items[-1].model_dump() == user_item("Current task").item.model_dump()


async def test_summary_provider_overflow_subdivides_complete_groups(store, tmp_path):
    session = await store.create_session(tmp_path)
    await populate_large_compaction_history(store, session)

    class RejectFirstSummary(ScriptedModel):
        async def stream(self, request):
            if not self.requests:
                self.requests.append(request)
                raise AgentError(ErrorCode.CONTEXT_WINDOW_EXCEEDED, "Provider overflow")
            async for event in super().stream(request):
                yield event

    model = RejectFirstSummary([text_response("Merged verified evidence")] * 8)
    runtime = AgentRuntime(small_compaction_config(), store, model, Tools())
    await runtime.compact(session, quiet)
    assert len(model.requests[1].items) < len(model.requests[0].items)
    active = await runtime.context.load(session, await store.read(session))
    assert active.epoch == 1


async def test_later_summary_batch_failure_preserves_original_checkpoint(store, tmp_path):
    session = await store.create_session(tmp_path)
    await populate_large_compaction_history(store, session)

    class FailLaterSummary(ScriptedModel):
        async def stream(self, request):
            if self.requests:
                self.requests.append(request)
                raise AgentError(ErrorCode.INVALID_SUMMARY, "Later summary failed")
            async for event in super().stream(request):
                yield event

    model = FailLaterSummary([text_response("First segment evidence")])
    runtime = AgentRuntime(small_compaction_config(), store, model, Tools())
    before = await runtime.context.load(session, await store.read(session))
    with pytest.raises(AgentError) as error:
        await runtime.compact(session, quiet)
    assert error.value.code == ErrorCode.INVALID_SUMMARY
    assert await runtime.context.load(session, await store.read(session)) == before
    assert not any(
        record.type == JournalEventType.COMPACTION_COMMITTED for record in await store.read(session)
    )


async def test_batched_summary_preserves_complete_native_tool_pairs(store, tmp_path):
    session = await store.create_session(tmp_path)
    await store.append(session, JournalEventType.USER_MESSAGE, user_item("Current task"))
    runtime = AgentRuntime(
        small_compaction_config(),
        store,
        ScriptedModel([text_response("Merged paired evidence")] * 8),
        Tools(),
    )
    for index in range(8):
        call_id = f"read-{index}"
        await store.append(
            session,
            JournalEventType.MODEL_RESPONSE_COMMITTED,
            ModelCommitted(response=tool_response((call_id,))),
        )
        await runtime._result(
            session,
            "previous",
            ToolResult(
                call_id=call_id,
                content=ToolOutputRange(
                    content="x" * (3000 if index < 7 else 100),
                    total_characters=3000 if index < 7 else 100,
                ),
            ),
        )
    await runtime.compact(session, quiet)
    assert len(runtime.model.requests) > 1
    summarized_calls = []
    for request in runtime.model.requests:
        validate_pairs(ContextWindow(items=request.items))
        summarized_calls.extend(
            item.call_id for item in request.items if isinstance(item, NativeFunctionCall)
        )
    assert summarized_calls == [f"read-{index}" for index in range(7)]
    active = await runtime.context.load(session, await store.read(session))
    validate_pairs(active)
    assert [item.call_id for item in active.items if isinstance(item, NativeFunctionCall)] == [
        "read-7"
    ]


async def test_cancellation_during_later_summary_batch_preserves_checkpoint(store, tmp_path):
    session = await store.create_session(tmp_path)
    await populate_large_compaction_history(store, session)
    entered = asyncio.Event()

    class WaitLaterSummary(ScriptedModel):
        async def stream(self, request):
            if self.requests:
                self.requests.append(request)
                entered.set()
                await asyncio.Event().wait()
            async for event in super().stream(request):
                yield event

    model = WaitLaterSummary([text_response("First segment evidence")])
    runtime = AgentRuntime(small_compaction_config(), store, model, Tools())
    before = await runtime.context.load(session, await store.read(session))
    task = asyncio.create_task(runtime.compact(session, quiet))
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await runtime.context.load(session, await store.read(session)) == before


async def test_summary_input_between_reserved_budget_and_full_window_is_accepted(store, tmp_path):
    session = await store.create_session(tmp_path)
    await store.append(session, JournalEventType.USER_MESSAGE, user_item("x" * 12500))
    await store.append(session, JournalEventType.USER_MESSAGE, user_item("Current task"))
    config = small_compaction_config()
    model = ScriptedModel([text_response("Preserved evidence")])
    runtime = AgentRuntime(config, store, model, Tools())
    await runtime.compact(session, quiet)
    assert len(model.requests) == 1
    request = model.requests[0]
    measured = runtime.context.tokens(
        ContextInput(instructions=request.instructions, tools=request.tools, items=request.items)
    )
    reserved_budget = (
        config.model.context_window
        - config.context.summary_max_output_tokens
        - max(
            config.context.reserve_min_tokens,
            int(config.model.context_window * config.context.reserve_ratio),
        )
    )
    assert reserved_budget < measured <= config.model.context_window
    active = await runtime.context.load(session, await store.read(session))
    assert active.epoch == 1
