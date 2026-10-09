import pytest

from agent_client.application.context import ContextManager, validate_pairs
from agent_client.application.prompts import prefix_revision, user_item
from agent_client.domain.configuration import AppConfig
from agent_client.domain.enums import JournalEventType, MessageRole, NativeItemType
from agent_client.domain.errors import AgentError
from agent_client.domain.events import JournalRecord
from agent_client.domain.models import Contract, ModelResponse, ToolCall, ToolResult, ToolSpec
from agent_client.domain.protocol import (
    ContentType,
    NativeContent,
    NativeFunctionCall,
    NativeFunctionOutput,
    NativeMessage,
)
from agent_client.domain.runtime import (
    ContextGroupKind,
    ContextInput,
    ContextWindow,
    ModelCommitted,
    PrefixSnapshot,
    ToolResultCommitted,
)
from agent_client.domain.tools import ReadFileArguments, ToolOutputRange
from agent_client.domain.workspace import ProcessResult


def test_pairing_rejects_missing_duplicate_and_orphan_results():
    call = NativeFunctionCall(
        type=NativeItemType.FUNCTION_CALL,
        call_id="a",
        name="read_file",
        arguments='{"path":"task.txt"}',
    )
    result = NativeFunctionOutput(
        type=NativeItemType.FUNCTION_CALL_OUTPUT, call_id="a", output="ok"
    )
    for items in [[call], [result], [call, result, result], [call, user_item("new").item]]:
        with pytest.raises(AgentError):
            validate_pairs(ContextWindow(items=items))
    assert validate_pairs(ContextWindow(items=[call, result])) == []


def test_estimate_counts_utf8_and_prefix_hash_is_deterministic():
    manager = ContextManager(AppConfig(), None)
    assert manager.tokens(
        ContextInput(instructions="", items=[user_item(chr(27721) * 50).item])
    ) > manager.tokens(ContextInput(instructions="", items=[user_item("a" * 50).item]))
    left = ToolSpec(name="read_file", description="read_file", parameters={"b": 1, "a": 2})
    right = ToolSpec(name="read_file", description="read_file", parameters={"a": 2, "b": 1})
    assert prefix_revision(PrefixSnapshot(instructions="rules", tools=[left])) == prefix_revision(
        PrefixSnapshot(instructions="rules", tools=[right])
    )
    assert prefix_revision(PrefixSnapshot(instructions="changed", tools=[left])) != prefix_revision(
        PrefixSnapshot(instructions="rules", tools=[left])
    )


def test_budget_reserves_output_and_safety_margin():
    manager = ContextManager(AppConfig(), None)
    assert (
        manager.input_limit
        < manager.config.model.context_window - manager.config.model.max_output_tokens
    )


def test_compaction_target_is_one_quarter_of_model_context_window():
    config = AppConfig()
    config.model.context_window = 256000
    manager = ContextManager(config, None)
    assert manager.compaction_target == 64000
    config.model.max_output_tokens *= 2
    config.context.reserve_min_tokens *= 2
    assert manager.compaction_target == 64000
    assert config.context.soft_ratio == 0.95


def journal_record(seq: int, kind: JournalEventType, payload: Contract) -> JournalRecord:
    return JournalRecord(
        session_id="session", seq=seq, event_id=f"event-{seq}", type=kind, payload=payload
    )


def call_group(call_ids: tuple[str, ...]) -> ModelCommitted:
    calls = [
        ToolCall(id=call_id, name="read_file", arguments=ReadFileArguments(path="task.txt"))
        for call_id in call_ids
    ]
    output = [
        NativeFunctionCall(
            type=NativeItemType.FUNCTION_CALL,
            call_id=call.id,
            name=call.name,
            arguments='{"path":"task.txt"}',
        )
        for call in calls
    ]
    output.append(
        NativeMessage(
            type=NativeItemType.MESSAGE,
            role=MessageRole.ASSISTANT,
            content=[NativeContent(type=ContentType.OUTPUT_TEXT, text="Trailing native message")],
        )
    )
    return ModelCommitted(
        response=ModelResponse(id="response-" + call_ids[0], calls=calls, output=output)
    )


def call_result(call_id: str) -> ToolResultCommitted:
    return ToolResultCommitted(
        result=ToolResult(
            call_id=call_id,
            content=ToolOutputRange(content="evidence", total_characters=len("evidence")),
        ),
        item=NativeFunctionOutput(
            type=NativeItemType.FUNCTION_CALL_OUTPUT, call_id=call_id, output="evidence"
        ),
    )


async def test_compaction_split_keeps_pending_batch_and_recent_native_group_intact():
    records = [
        journal_record(1, JournalEventType.USER_MESSAGE, user_item("Current task")),
        journal_record(2, JournalEventType.MODEL_RESPONSE_COMMITTED, call_group(("old",))),
        journal_record(3, JournalEventType.TOOL_RESULT_COMMITTED, call_result("old")),
        journal_record(4, JournalEventType.MODEL_RESPONSE_COMMITTED, call_group(("recent",))),
        journal_record(5, JournalEventType.TOOL_RESULT_COMMITTED, call_result("recent")),
        journal_record(
            6, JournalEventType.MODEL_RESPONSE_COMMITTED, call_group(("pending-a", "pending-b"))
        ),
        journal_record(7, JournalEventType.TOOL_RESULT_COMMITTED, call_result("pending-a")),
    ]
    manager = ContextManager(AppConfig(), None)
    active = await manager.load("session", records)
    assert active.groups[-1].kind == ContextGroupKind.MODEL_STEP
    assert not active.groups[-1].complete
    split = manager.retained_tail(active)
    assert split.current_request.items == [user_item("Current task").item]
    assert validate_pairs(split.older) == []
    assert validate_pairs(split.tail, allow_pending=True) == ["pending-b"]
    assert [item.call_id for item in split.older.items if isinstance(item, NativeFunctionCall)] == [
        "old"
    ]
    assert [item.call_id for item in split.tail.items if isinstance(item, NativeFunctionCall)] == [
        "recent",
        "pending-a",
        "pending-b",
    ]
    assert split.tail.items.count(user_item("Current task").item) == 1


def test_legacy_checkpoint_uses_whole_user_interactions_without_item_level_cuts():
    old = call_group(("legacy",)).response.output
    window = ContextWindow(
        items=[
            user_item("Old task").item,
            *old,
            call_result("legacy").item,
            user_item("Current task").item,
        ]
    )
    split = ContextManager(AppConfig(), None).retained_tail(window)
    assert split.older.items == window.items[:-1]
    assert split.tail.items == [user_item("Current task").item]
    assert split.older.groups[0].kind == ContextGroupKind.USER_INTERACTION
    validate_pairs(split.older)


async def test_unsettled_background_process_group_is_retained_until_terminal_poll():
    background = call_result("background")
    background.result.content = ProcessResult(process_handle="owned", running=True)
    records = [
        journal_record(1, JournalEventType.USER_MESSAGE, user_item("Current task")),
        journal_record(2, JournalEventType.MODEL_RESPONSE_COMMITTED, call_group(("background",))),
        journal_record(3, JournalEventType.TOOL_RESULT_COMMITTED, background),
        journal_record(4, JournalEventType.MODEL_RESPONSE_COMMITTED, call_group(("old-read",))),
        journal_record(5, JournalEventType.TOOL_RESULT_COMMITTED, call_result("old-read")),
        journal_record(6, JournalEventType.MODEL_RESPONSE_COMMITTED, call_group(("recent-read",))),
        journal_record(7, JournalEventType.TOOL_RESULT_COMMITTED, call_result("recent-read")),
    ]
    manager = ContextManager(AppConfig(), None)
    active = await manager.load("session", records)
    split = manager.retained_tail(active)
    assert active.groups[1].pending_processes == {"owned"}
    assert [item.call_id for item in split.older.items if isinstance(item, NativeFunctionCall)] == [
        "old-read"
    ]
    assert [item.call_id for item in split.tail.items if isinstance(item, NativeFunctionCall)] == [
        "background",
        "recent-read",
    ]
    validate_pairs(split.tail)
    terminal = call_result("poll")
    terminal.result.content = ProcessResult(process_handle="owned", running=False, exit_code=0)
    records.extend(
        [
            journal_record(8, JournalEventType.MODEL_RESPONSE_COMMITTED, call_group(("poll",))),
            journal_record(9, JournalEventType.TOOL_RESULT_COMMITTED, terminal),
        ]
    )
    settled = await manager.load("session", records)
    assert not settled.groups[1].pending_processes
    settled_split = manager.retained_tail(settled)
    assert "background" in [
        item.call_id for item in settled_split.older.items if isinstance(item, NativeFunctionCall)
    ]
    validate_pairs(settled_split.older)
