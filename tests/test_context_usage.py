import pytest
from pydantic import ValidationError

from agent_client.application.context import ContextManager
from agent_client.application.prompts import user_item
from agent_client.domain.configuration import AppConfig
from agent_client.domain.context_usage import ContextUsage
from agent_client.domain.enums import (
    CompactionReason,
    ContextStrategy,
    JournalEventType,
    NativeItemType,
    RunStatus,
    TokenMeasurement,
)
from agent_client.domain.models import ModelResponse, ToolResult, ToolSpec
from agent_client.domain.protocol import (
    NativeFunctionCall,
    NativeFunctionOutput,
    NativeMessage,
    NativeReasoning,
    TokenDetails,
    TokenUsage,
)
from agent_client.domain.runtime import (
    CompactionCommitted,
    ContextInput,
    ContextWindow,
    ModelCommitted,
    PrefixSnapshot,
    RunStarted,
    ToolResultCommitted,
)
from agent_client.domain.tools import ToolOutputRange
from agent_client.infrastructure.persistence.store import SessionStore


def manager() -> ContextManager:
    return ContextManager(AppConfig(), None)


def test_unknown_context_usage_stays_unknown_and_over_budget_usage_is_visible():
    unknown = ContextUsage(context_window=256000, input_budget=235008)
    assert unknown.used_tokens is None
    assert unknown.ratio is None
    assert unknown.measurement == TokenMeasurement.UNAVAILABLE
    over = ContextUsage(
        used_tokens=260000,
        context_window=256000,
        input_budget=235008,
        measurement=TokenMeasurement.UTF8_BYTE_ESTIMATE,
    )
    assert over.ratio > 1
    with pytest.raises(ValidationError):
        ContextUsage(used_tokens=0, context_window=256000, input_budget=235008)
    with pytest.raises(ValidationError):
        ContextUsage(context_window=256000, input_budget=256001)


def test_context_snapshot_includes_current_instructions_tools_and_history():
    context = manager()
    base = ContextInput(instructions="Help")
    with_tools = ContextInput(
        instructions="Help",
        tools=[
            ToolSpec(name="read_file", description="Read a file", parameters={"type": "object"})
        ],
    )
    current = with_tools.model_copy(update={"items": [user_item("Explain this file " * 100).item]})
    assert (
        context.usage(base).used_tokens
        < context.usage(with_tools).used_tokens
        < context.usage(current).used_tokens
    )
    measured = context.usage(current)
    assert measured.used_tokens == context.tokens(current)
    assert measured.context_window == context.config.model.context_window
    assert measured.input_budget == context.input_limit
    assert measured.measurement == TokenMeasurement.UTF8_BYTE_ESTIMATE
    assert context.usage(current).used_tokens == measured.used_tokens


def test_provider_usage_anchors_current_step_and_cache_is_not_subtracted():
    context = manager()
    request = ContextInput(instructions="Help", items=[user_item("Question").item])
    response = ModelResponse(
        id="first",
        output=[NativeReasoning(type=NativeItemType.REASONING, encrypted_content="opaque" * 10000)],
        usage=TokenUsage(
            input_tokens=1000,
            output_tokens=200,
            input_tokens_details=TokenDetails(cached_tokens=900),
        ),
    )
    first = context.usage_after_response(request, response)
    assert first.used_tokens == 1200
    assert first.measurement == TokenMeasurement.PROVIDER_USAGE_WITH_ESTIMATE
    response = ModelResponse(
        id="second", output=[], usage=TokenUsage(input_tokens=1700, output_tokens=30)
    )
    assert context.usage_after_response(request, response).used_tokens == 1730


def test_reported_context_uses_only_provider_input_and_keeps_missing_usage_unknown():
    context = manager()
    assert context.reported_usage().used_tokens is None
    response = ModelResponse(
        id="step", output=[], usage=TokenUsage(input_tokens=1200, output_tokens=400)
    )
    measured = context.reported_usage(response)
    assert measured.used_tokens == 1200
    assert measured.measurement == TokenMeasurement.PROVIDER_INPUT_TOKENS
    assert context.reported_usage(ModelResponse(id="missing", output=[])).used_tokens is None


def test_missing_usage_estimates_current_payload_and_tool_tail_without_resetting_anchor():
    context = manager()
    request = ContextInput(instructions="Help", items=[user_item("Question").item])
    output = [NativeMessage(role="assistant", content="Answer " * 100)]
    absent = ModelResponse(id="absent", output=output)
    after = request.model_copy(update={"items": [*request.items, *output]})
    assert context.usage_after_response(request, absent) == context.usage(after)
    anchored = context.usage_after_response(
        request, ModelResponse(id="partial", output=output, usage=TokenUsage(input_tokens=500))
    )
    assert anchored.used_tokens > 500
    tail = after.model_copy(
        update={
            "items": [
                *after.items,
                NativeFunctionOutput(
                    type=NativeItemType.FUNCTION_CALL_OUTPUT,
                    call_id="read_file",
                    output="File contents " * 200,
                ),
            ]
        }
    )
    extended = context.extend_usage(anchored, after, tail)
    assert extended.used_tokens > anchored.used_tokens
    assert extended.measurement == TokenMeasurement.PROVIDER_USAGE_WITH_ESTIMATE
    assert context.extend_usage(extended, tail, tail) == extended
    with pytest.raises(ValueError, match="unchanged prefix"):
        context.extend_usage(extended, tail, request)


@pytest.mark.parametrize("invalid", [-1, True, "200", 1.5])
def test_invalid_explicit_provider_token_usage_is_not_silently_estimated(invalid):
    context = manager()
    with pytest.raises(ValueError, match="greater than or equal|valid integer"):
        context.usage_after_response(
            ContextInput(instructions="Help"),
            ModelResponse(id="bad", output=[], usage=TokenUsage(input_tokens=invalid)),
        )


async def test_restored_context_uses_current_compaction_epoch_instead_of_cumulative_usage(tmp_path):
    config = AppConfig()
    store = await SessionStore(tmp_path / "home").open()
    try:
        session = await store.create_session(tmp_path)
        context = ContextManager(config, store)
        prefix = PrefixSnapshot(
            instructions="Current instructions",
            tools=[ToolSpec(name="read_file", description="Read", parameters={"type": "object"})],
        )
        await store.append(
            session,
            JournalEventType.RUN_STARTED,
            RunStarted(
                status=RunStatus.RUNNING,
                model=config.model.model,
                prefix_revision="prefix",
                instructions=prefix.instructions,
                tools=prefix.tools,
            ),
            run_id="original",
        )
        response = ModelResponse(
            id="old",
            output=[NativeMessage(role="assistant", content="Old context " * 1000)],
            usage=TokenUsage(input_tokens=100000, output_tokens=50000),
        )
        await store.append(
            session, JournalEventType.MODEL_RESPONSE_COMMITTED, ModelCommitted(response=response)
        )
        before = await context.session_usage(session, prefix, await store.read(session))
        assert before.used_tokens == 150000
        assert before.measurement == TokenMeasurement.PROVIDER_USAGE_WITH_ESTIMATE
        snapshot = ContextWindow(items=[NativeMessage(role="assistant", content="Compact summary")])
        artifact = await store.put_artifact(session, snapshot.model_dump_json())
        await store.append(
            session,
            JournalEventType.COMPACTION_COMMITTED,
            CompactionCommitted(
                epoch=1,
                source_seq=1,
                artifact_id=artifact,
                strategy=ContextStrategy.SUMMARY,
                before_tokens=before.used_tokens,
                after_tokens=50,
                reason=CompactionReason.MANUAL,
            ),
        )
        await store.close()
        await store.open()
        after = await context.session_usage(session, prefix, await store.recover(session))
        assert after.used_tokens < before.used_tokens
        assert after.used_tokens == context.tokens(
            ContextInput(instructions=prefix.instructions, tools=prefix.tools, items=snapshot.items)
        )
        assert after.measurement == TokenMeasurement.UTF8_BYTE_ESTIMATE
        assert after.used_tokens != 150000
    finally:
        await store.close()


async def test_restored_tool_results_extend_provider_anchor_without_counting_cache_or_cipher_bytes(
    tmp_path,
):
    config = AppConfig()
    store = await SessionStore(tmp_path / "home").open()
    try:
        session = await store.create_session(tmp_path)
        context = ContextManager(config, store)
        prefix = PrefixSnapshot(instructions="Help")
        await store.append(
            session,
            JournalEventType.RUN_STARTED,
            RunStarted(
                status=RunStatus.RUNNING,
                model=config.model.model,
                prefix_revision="prefix",
                instructions=prefix.instructions,
                tools=prefix.tools,
            ),
            run_id="original",
        )
        user = user_item("Inspect file")
        await store.append(session, JournalEventType.USER_MESSAGE, user, run_id="original")
        response = ModelResponse(
            id="anchored",
            output=[
                NativeReasoning(
                    type=NativeItemType.REASONING, id="reason", encrypted_content="opaque" * 10000
                ),
                NativeFunctionCall(
                    type=NativeItemType.FUNCTION_CALL,
                    call_id="read_file",
                    name="read_file",
                    arguments='{"path":"task.txt"}',
                ),
            ],
            usage=TokenUsage(
                input_tokens=1000,
                output_tokens=200,
                input_tokens_details=TokenDetails(cached_tokens=900),
            ),
        )
        await store.append(
            session,
            JournalEventType.MODEL_RESPONSE_COMMITTED,
            ModelCommitted(response=response),
            run_id="original",
        )
        before = await context.session_usage(session, prefix, await store.read(session))
        assert before.used_tokens == 1200
        result = ToolResult(
            call_id="read_file",
            content=ToolOutputRange(
                content="File contents " * 100, total_characters=len("File contents " * 100)
            ),
        )
        item = NativeFunctionOutput(
            type=NativeItemType.FUNCTION_CALL_OUTPUT,
            call_id=result.call_id,
            output=result.content.content,
        )
        await store.append(
            session,
            JournalEventType.TOOL_RESULT_COMMITTED,
            ToolResultCommitted(result=result, item=item),
            run_id="original",
        )
        live = await context.session_usage(session, prefix, await store.read(session))
        assert 1200 < live.used_tokens < 4000
        assert live.measurement == TokenMeasurement.PROVIDER_USAGE_WITH_ESTIMATE
        await store.close()
        await store.open()
        restored = await context.session_usage(session, prefix, await store.read(session))
        assert restored == live
        changed = PrefixSnapshot(instructions="Changed instructions")
        changed_usage = await context.session_usage(session, changed, await store.read(session))
        assert changed_usage.measurement == TokenMeasurement.PROVIDER_USAGE_WITH_ESTIMATE
        assert changed_usage.used_tokens > live.used_tokens
        await store.append(
            session,
            JournalEventType.RUN_STARTED,
            RunStarted(
                status=RunStatus.RUNNING,
                model=config.model.model,
                prefix_revision="new-prefix",
                instructions=prefix.instructions,
                tools=prefix.tools,
            ),
            run_id="next",
        )
        new_run_usage = await context.session_usage(session, prefix, await store.read(session))
        assert new_run_usage.measurement == TokenMeasurement.PROVIDER_USAGE_WITH_ESTIMATE
        assert new_run_usage == live
    finally:
        await store.close()


@pytest.mark.parametrize("window", [256000, 512000])
@pytest.mark.parametrize("ratio, expected_compactions", [(0.94, 0), (0.951, 1)])
async def test_runtime_compaction_uses_provider_anchored_full_window_ratio(
    tmp_path, window, ratio, expected_compactions
):
    from agent_client.application.runtime import AgentRuntime
    from agent_client.domain.runtime import RunEnvironment

    config = AppConfig()
    config.model.context_window = window
    store = await SessionStore(tmp_path / "home").open()
    session = await store.create_session(tmp_path)
    prefix = PrefixSnapshot(instructions="Help")
    await store.append(
        session,
        JournalEventType.RUN_STARTED,
        RunStarted(
            status=RunStatus.RUNNING,
            model=config.model.model,
            prefix_revision="prefix",
            instructions=prefix.instructions,
            tools=prefix.tools,
        ),
    )
    await store.append(session, JournalEventType.USER_MESSAGE, user_item("Inspect current task"))
    response = ModelResponse(
        id="anchor",
        output=[NativeReasoning(encrypted_content="opaque" * 100000)],
        usage=TokenUsage(input_tokens=int(window * ratio), output_tokens=0),
    )
    await store.append(
        session, JournalEventType.MODEL_RESPONSE_COMMITTED, ModelCommitted(response=response)
    )

    class CountingRuntime(AgentRuntime):
        async def _compact(self, environment, emit, reason):
            self.compactions.append(reason)

    async def emit(event):
        return None

    runtime = CountingRuntime(config, store, None, None)
    runtime.compactions = []
    environment = RunEnvironment(
        session_id=session,
        run_id="run",
        workspace=tmp_path,
        instructions=prefix.instructions,
        tools=prefix.tools,
        prefix_revision="prefix",
    )
    try:
        active = await runtime._prepare_context(environment, emit)
        request = ContextInput(
            instructions=prefix.instructions, tools=prefix.tools, items=active.items
        )
        assert runtime.context.tokens(request) == int(window * ratio)
        assert runtime.context.usage(request).used_tokens == int(window * ratio)
        assert len(runtime.compactions) == expected_compactions
    finally:
        await store.close()


def test_provider_anchor_does_not_oscillate_between_stream_steps():
    context = manager()
    request = ContextInput(instructions="Help", items=[user_item("Inspect").item])
    response = ModelResponse(
        id="response",
        output=[NativeReasoning(encrypted_content="opaque" * 10000)],
        usage=TokenUsage(input_tokens=1000, output_tokens=200),
    )
    complete = context.usage_after_response(request, response)
    current = ContextInput(
        instructions=request.instructions,
        tools=request.tools,
        items=[*request.items, *response.output],
    )
    assert context.usage(current) == complete
    assert context.tokens(current) == complete.used_tokens
    extended = ContextInput(
        instructions=current.instructions,
        tools=current.tools,
        items=[*current.items, NativeFunctionOutput(call_id="tool", output="file contents" * 100)],
    )
    after_tool = context.usage(extended)
    assert after_tool.used_tokens > complete.used_tokens
    assert context.usage(extended) == after_tool
    assert context.tokens(extended) == after_tool.used_tokens


def test_native_summary_budget_uses_full_provider_window_bound_without_reencoding_history():
    context = manager()
    request = ContextInput(instructions="Coding rules", items=[user_item("Current goal").item])
    response = ModelResponse(
        id="response",
        output=[NativeReasoning(encrypted_content="opaque" * 100000)],
        usage=TokenUsage(input_tokens=240000, output_tokens=3800),
    )
    complete = context.usage_after_response(request, response)
    source = ContextInput(
        instructions=request.instructions,
        tools=request.tools,
        items=[*request.items, *response.output],
    )
    summary = ContextInput(
        instructions="Summarize existing history", items=[response.output[0], request.items[0]]
    )
    assert context.tokens(summary) > context.input_limit
    bounded = context.summary_tokens(source, summary)
    assert complete.used_tokens <= bounded < context.input_limit
    invalid = ContextInput(
        instructions=summary.instructions,
        items=[*summary.items, user_item("Invented history").item],
    )
    with pytest.raises(ValueError, match="only existing context"):
        context.summary_tokens(source, invalid)


def test_summary_synthesized_final_instruction_is_budgeted_and_history_remains_strict():
    context = manager()
    source = ContextInput(instructions="Coding rules", items=[user_item("Original goal").item])
    context.usage_after_response(
        source,
        ModelResponse(
            id="anchor", output=[], usage=TokenUsage(input_tokens=1000, output_tokens=100)
        ),
    )
    instruction = user_item("Summarize the handoff " * 200).item
    request = ContextInput(instructions=source.instructions, items=[*source.items, instruction])
    bounded = context.summary_tokens(source, request, synthesized_input=instruction)
    assert bounded > context.summary_tokens(source, source)
    with pytest.raises(ValueError, match="only existing context"):
        context.summary_tokens(source, request)
    misplaced = ContextInput(instructions=source.instructions, items=[instruction, *source.items])
    with pytest.raises(ValueError, match="final input"):
        context.summary_tokens(source, misplaced, synthesized_input=instruction)
