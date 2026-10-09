from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Awaitable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast
from uuid import uuid4

from pydantic import ValidationError

from agent_client.application.context import ContextManager, validate_pairs
from agent_client.application.prompts import (
    BASE_INSTRUCTIONS,
    SUMMARY_INSTRUCTIONS,
    SUMMARY_REQUEST,
    canonical_json,
    prefix_revision,
    user_item,
)
from agent_client.domain.configuration import AppConfig
from agent_client.domain.context_usage import ContextUsage
from agent_client.domain.enums import (
    AuthMode,
    CompactionReason,
    ContextStrategy,
    ErrorCode,
    JournalEventType,
    ModelEventKind,
    ModelResponseStatus,
    ProviderKind,
    RunStatus,
    RuntimeEventKind,
    StopReason,
    ToolExecutionState,
    ToolStatus,
)
from agent_client.domain.errors import AgentError
from agent_client.domain.events import EventSink, JournalRecord, RuntimeEvent
from agent_client.domain.models import (
    ApprovalHandler,
    ApprovalRequest,
    Contract,
    Effect,
    ModelRequest,
    ModelResponse,
    RunResult,
    ToolCall,
    ToolContext,
    ToolResult,
)
from agent_client.domain.protocol import NativeFunctionCall, NativeFunctionOutput
from agent_client.domain.runtime import (
    ApprovalDecision,
    BackgroundProcessSettled,
    CompactionCommitted,
    CompactionStarted,
    ContextGroup,
    ContextGroupKind,
    ContextInput,
    ContextWindow,
    ContinuationAction,
    ContinuationPlan,
    ConversationSummary,
    EpochChanged,
    ErrorOccurred,
    InputDisposition,
    InputSubmission,
    ModelCommitted,
    ModelCompleted,
    ModelExchange,
    ModelFailure,
    ModelIncomplete,
    ModelMetrics,
    ModelRequestMetadata,
    PendingInput,
    PrefixSnapshot,
    RunEnvironment,
    RunFinished,
    RunProgress,
    RunStarted,
    TextDelta,
    ToolFinished,
    ToolResultCommitted,
    ToolStateChange,
    UnknownResolution,
    UserMessage,
)
from agent_client.domain.tools import (
    ToolDispatchEvent,
    ToolErrorContent,
    ToolOutputProjection,
    argument_model,
)
from agent_client.domain.workspace import ProcessResult

if TYPE_CHECKING:
    from agent_client.application.tools import ToolService
    from agent_client.domain.ports import ModelGateway
    from agent_client.infrastructure.persistence.store import SessionStore


async def await_owned_completion[T](operation: Awaitable[T]) -> T:
    task = asyncio.ensure_future(operation)
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


@dataclass
class ActiveInputTarget:
    session_id: str
    run_id: str
    accepting: bool = True


class AgentRuntime:
    def __init__(
        self, config: AppConfig, store: SessionStore, model: ModelGateway, tools: ToolService
    ):
        self.config = config
        self.store = store
        self.model = model
        self.tools = tools
        self.context = ContextManager(config, store)
        self._input_lock = asyncio.Lock()
        self._input_targets: list[ActiveInputTarget] = []

    def validate_provider(self, records: list[JournalRecord]) -> None:
        previous = next(
            (
                cast(RunStarted, record.payload)
                for record in reversed(records)
                if record.type == JournalEventType.RUN_STARTED
            ),
            None,
        )
        if previous is None:
            return
        config = self.config.model
        if previous.base_url is None or previous.auth_mode is None:
            if (
                previous.provider != ProviderKind.OPENAI_RESPONSES
                or (
                    previous.base_url is not None
                    and previous.base_url.rstrip("/") != "https://api.openai.com/v1"
                )
                or previous.auth_mode not in {None, AuthMode.CHATGPT}
                or config.provider != ProviderKind.OPENAI_RESPONSES
                or config.base_url.rstrip("/") != "https://api.openai.com/v1"
                or config.auth_mode != AuthMode.CHATGPT
            ):
                raise AgentError(
                    ErrorCode.INVALID_CONTEXT,
                    "This session has no complete provider binding; only the original official ChatGPT configuration can resume it",
                )
        if (
            previous.provider != config.provider
            or (
                previous.base_url is not None
                and previous.base_url.rstrip("/") != config.base_url.rstrip("/")
            )
            or (previous.auth_mode is not None and previous.auth_mode != config.auth_mode)
        ):
            raise AgentError(
                ErrorCode.INVALID_CONTEXT,
                "This session belongs to another provider or endpoint; start a new session with the selected configuration",
            )

    async def enqueue(self, session_id: str, prompt: str, command_id: str | None = None) -> str:
        identity = command_id or str(uuid4())
        await self.store.append(
            session_id,
            JournalEventType.PENDING_INPUT,
            PendingInput(command_id=identity, prompt=prompt),
            event_id="pending:" + identity,
        )
        return identity

    async def steer(
        self,
        session_id: str,
        prompt: str,
        expected_run_id: str,
        command_id: str | None = None,
    ) -> InputSubmission:
        identity = command_id or str(uuid4())
        async with self._input_lock:
            target = next(
                (
                    target
                    for target in self._input_targets
                    if target.session_id == session_id
                    and target.run_id == expected_run_id
                    and target.accepting
                ),
                None,
            )
            pending = PendingInput(
                command_id=identity,
                prompt=prompt,
                target_run_id=target.run_id if target is not None else None,
            )
            await await_owned_completion(
                self.store.append(
                    session_id,
                    JournalEventType.PENDING_INPUT,
                    pending,
                    event_id="pending:" + identity,
                )
            )
            return InputSubmission(
                command_id=identity,
                disposition=InputDisposition.STEERED
                if target is not None
                else InputDisposition.QUEUED,
                run_id=pending.target_run_id,
            )

    async def _consume_steering(
        self, environment: RunEnvironment, emit: EventSink, close_if_empty: bool = False
    ) -> bool:
        async with self._input_lock:
            pending = [
                item
                for item in await self.pending_inputs(environment.session_id)
                if item.target_run_id == environment.run_id
            ]
            for item in pending:
                message = user_item(item.prompt)
                message.command_id = item.command_id
                await await_owned_completion(
                    self.store.append(
                        environment.session_id,
                        JournalEventType.USER_MESSAGE,
                        message,
                        run_id=environment.run_id,
                        event_id="consumed:" + item.command_id,
                    )
                )
                await self._notify(
                    emit,
                    RuntimeEventKind.INPUT_STEERED,
                    environment.session_id,
                    environment.run_id,
                    message,
                )
            if close_if_empty and not pending:
                for target in self._input_targets:
                    if target.run_id == environment.run_id:
                        target.accepting = False
            return bool(pending)

    async def pending_inputs(self, session_id: str) -> list[PendingInput]:
        records = await self.store.read(session_id)
        consumed = {
            cast(UserMessage, record.payload).command_id
            for record in records
            if record.type == JournalEventType.USER_MESSAGE
        }
        pending = [
            cast(PendingInput, record.payload)
            for record in records
            if record.type == JournalEventType.PENDING_INPUT
        ]
        return [item for item in pending if item.command_id not in consumed]

    async def prepare_continuation(self, session_id: str) -> ContinuationPlan:
        async with self.store.session_lock(session_id):
            records = await self.store.recover(session_id)
            await self._recover(session_id, records)
            records = await self.store.read(session_id)
            unknown = sorted(self._unresolved_unknown(records))
            pending = await self.pending_inputs(session_id)
            if pending:
                return ContinuationPlan(
                    action=ContinuationAction.QUEUED, inputs=pending, unresolved_call_ids=unknown
                )
            latest = next(
                (
                    record
                    for record in reversed(records)
                    if record.type == JournalEventType.RUN_STARTED
                ),
                None,
            )
            if latest is None:
                return ContinuationPlan(
                    action=ContinuationAction.NO_TASK, unresolved_call_ids=unknown
                )
            terminal = next(
                (
                    cast(RunFinished, record.payload)
                    for record in reversed(records)
                    if record.type == JournalEventType.RUN_FINISHED
                    and record.run_id == latest.run_id
                ),
                None,
            )
            if terminal is None:
                raise AgentError(ErrorCode.JOURNAL_CORRUPT, "Recovered run has no terminal state")
            match terminal.status:
                case (
                    RunStatus.FAILED
                    | RunStatus.CANCELLED
                    | RunStatus.PARTIAL
                    | RunStatus.INTERRUPTED
                ):
                    item = PendingInput(
                        command_id=str(uuid4()),
                        prompt="Continue the unfinished task using the committed conversation and tool results. Respect the original request and constraints. Do not repeat completed actions without a new reason.",
                        continuation_of=latest.run_id,
                    )
                    await self.store.append(
                        session_id,
                        JournalEventType.PENDING_INPUT,
                        item,
                        event_id="pending:" + item.command_id,
                    )
                    return ContinuationPlan(
                        action=ContinuationAction.PREPARED,
                        inputs=[item],
                        continuation_of=latest.run_id,
                        unresolved_call_ids=unknown,
                    )
                case _:
                    return ContinuationPlan(
                        action=ContinuationAction.NO_TASK, unresolved_call_ids=unknown
                    )

    async def _running_processes(self, session_id: str) -> set[str]:
        handles: set[str] = set()
        for record in await self.store.read(session_id):
            match record.type:
                case JournalEventType.BACKGROUND_PROCESS_SETTLED:
                    settled = cast(BackgroundProcessSettled, record.payload)
                    handles.discard(settled.process_handle)
                case JournalEventType.TOOL_RESULT_COMMITTED:
                    result = cast(ToolResultCommitted, record.payload).result
                    content = result.content
                    if (
                        isinstance(content, ProcessResult | ToolOutputProjection)
                        and content.process_handle is not None
                    ):
                        handle = content.process_handle
                        match content.running:
                            case True:
                                handles.add(handle)
                            case False:
                                handles.discard(handle)
                case _:
                    pass
        return handles

    def _unresolved_unknown(self, records: list[JournalRecord]) -> set[str]:
        resolved: set[str] = set()
        unknown: set[str] = set()
        for record in records:
            match record.type:
                case JournalEventType.UNKNOWN_OUTCOME_RESOLVED:
                    resolved.add(cast(UnknownResolution, record.payload).call_id)
                case JournalEventType.TOOL_RESULT_COMMITTED:
                    result = cast(ToolResultCommitted, record.payload).result
                    if result.status == ToolStatus.UNKNOWN:
                        unknown.add(result.call_id)
                case JournalEventType.BACKGROUND_PROCESS_SETTLED:
                    settled = cast(BackgroundProcessSettled, record.payload)
                    if settled.result.side_effect_result_unknown:
                        unknown.add(settled.call_id)
                case _:
                    pass
        return unknown - resolved

    async def _settle_process(
        self, session_id: str, run_id: str | None, call_id: str, result: ProcessResult
    ) -> None:
        if result.process_handle is None:
            raise AgentError(ErrorCode.TOOL_PROTOCOL, "Process result has no handle")
        payload = BackgroundProcessSettled(
            call_id=call_id,
            process_handle=result.process_handle,
            result=result,
            item=user_item("Owned process outcome: " + canonical_json(result)).item,
        )
        await self.store.append(
            session_id, JournalEventType.BACKGROUND_PROCESS_SETTLED, payload, run_id=run_id
        )
        match result:
            case ProcessResult(side_effect_result_unknown=True):
                state = ToolExecutionState.UNKNOWN
            case ProcessResult(cancelled=True):
                state = ToolExecutionState.CANCELLED
            case ProcessResult(exit_code=0):
                state = ToolExecutionState.SUCCEEDED
            case _:
                state = ToolExecutionState.FAILED
        await self.store.append(
            session_id,
            JournalEventType.TOOL_CALL_STATE,
            ToolStateChange(call_id=call_id, state=state),
            run_id=run_id,
        )
        self.tools.release_result(result)

    async def _cancel_processes(self, session_id: str, run_id: str) -> None:
        results = await self.tools.cancel_session(session_id)
        records = await self.store.read(session_id)
        handle_calls: list[tuple[str, str]] = []
        for record in records:
            if record.type == JournalEventType.TOOL_RESULT_COMMITTED:
                result = cast(ToolResultCommitted, record.payload).result
                content = result.content
                if (
                    isinstance(content, ProcessResult | ToolOutputProjection)
                    and content.process_handle is not None
                ):
                    handle_calls.append((content.process_handle, result.call_id))
        running = await self._running_processes(session_id)
        pending = set(
            validate_pairs(await self.context.load(session_id, records), allow_pending=True)
        )
        for result in results:
            if result.tool_call_id in pending:
                match result:
                    case ProcessResult(side_effect_result_unknown=True):
                        status = ToolStatus.UNKNOWN
                    case ProcessResult(cancelled=True):
                        status = ToolStatus.CANCELLED
                    case ProcessResult(exit_code=0):
                        status = ToolStatus.SUCCEEDED
                    case _:
                        status = ToolStatus.FAILED
                await self._result(
                    session_id,
                    run_id,
                    ToolResult(
                        call_id=result.tool_call_id,
                        content=result,
                        status=status,
                        is_error=status in {ToolStatus.UNKNOWN, ToolStatus.FAILED},
                    ),
                )
                pending.remove(result.tool_call_id)
            if result.process_handle in running:
                await self._settle_process(
                    session_id,
                    run_id,
                    next(
                        call_id
                        for handle, call_id in reversed(handle_calls)
                        if handle == result.process_handle
                    ),
                    result,
                )

    async def resolve_unknown(
        self,
        session_id: str,
        call_id: str,
        resolution: Literal[ToolStatus.FAILED, ToolStatus.SUCCEEDED],
        note: str,
    ) -> None:
        if not note.strip():
            raise AgentError(
                ErrorCode.INVALID_RESOLUTION, "Reconciliation requires evidence or a decision note"
            )
        async with self.store.session_lock(session_id):
            records = await self.store.recover(session_id)
            states = [
                cast(ToolStateChange, record.payload)
                for record in records
                if record.type == JournalEventType.TOOL_CALL_STATE
            ]
            state = next((item.state for item in reversed(states) if item.call_id == call_id), None)
            if state not in {
                ToolExecutionState.UNKNOWN,
                ToolExecutionState.DISPATCHING,
                ToolExecutionState.RUNNING,
            }:
                raise AgentError(
                    ErrorCode.INVALID_RESOLUTION, "Tool call has no unresolved unknown outcome"
                )
            active = await self.context.load(session_id, records)
            if call_id in validate_pairs(active, allow_pending=True):
                await self._recover(session_id, records)
            payload = UnknownResolution(
                call_id=call_id,
                resolution=resolution,
                note=note,
                item=user_item(
                    f"User reconciliation of tool {call_id}: {resolution}. Evidence/decision: {note}. Do not replay this action."
                ).item,
            )
            await self.store.append(session_id, JournalEventType.UNKNOWN_OUTCOME_RESOLVED, payload)
            await self.store.append(
                session_id,
                JournalEventType.TOOL_CALL_STATE,
                ToolStateChange(call_id=call_id, state=ToolExecutionState[resolution.name]),
            )

    async def _notify(
        self,
        emit: EventSink,
        kind: RuntimeEventKind,
        session_id: str,
        run_id: str,
        data: Contract | None = None,
    ) -> None:
        await emit(
            RuntimeEvent(
                kind=kind,
                session_id=session_id,
                run_id=run_id,
                data=data,
            )
        )

    async def _response(
        self, request: ModelRequest, emit: EventSink, session_id: str, run_id: str
    ) -> ModelExchange:
        response: ModelResponse | None = None
        started = time.monotonic()
        first_token: float | None = None
        async for event in self.model.stream(request):
            match event.kind:
                case ModelEventKind.REASONING_DELTA:
                    if first_token is None:
                        first_token = time.monotonic() - started
                    await self._notify(
                        emit, RuntimeEventKind.REASONING_DELTA, session_id, run_id, event.reasoning
                    )
                case ModelEventKind.TEXT_DELTA:
                    if first_token is None:
                        first_token = time.monotonic() - started
                    await self._notify(
                        emit,
                        RuntimeEventKind.TEXT_DELTA,
                        session_id,
                        run_id,
                        TextDelta(text=event.text),
                    )
                case ModelEventKind.COMPLETED:
                    if response is not None or event.response is None:
                        raise AgentError(
                            ErrorCode.MODEL_PROTOCOL, "Invalid completed response event"
                        )
                    response = event.response
        if response is None:
            raise AgentError(
                ErrorCode.MODEL_INCOMPLETE, "Model stream ended without a completed response"
            )
        metrics = ModelMetrics(
            duration_seconds=time.monotonic() - started,
            ttft_seconds=first_token,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cached_input_tokens=response.usage.input_tokens_details.cached_tokens
            if response.usage.input_tokens_details is not None
            else None,
        )
        return ModelExchange(response=response, metrics=metrics)

    async def _result(self, session_id: str, run_id: str, result: ToolResult) -> None:
        projected = await self.tools.project_output(session_id, result)
        payload = ToolResultCommitted(
            result=result,
            item=NativeFunctionOutput(call_id=result.call_id, output=projected),
        )
        await self.store.append(
            session_id, JournalEventType.TOOL_RESULT_COMMITTED, payload, run_id=run_id
        )
        await self.store.append(
            session_id,
            JournalEventType.TOOL_CALL_STATE,
            ToolStateChange(call_id=result.call_id, state=ToolExecutionState[result.status.name]),
            run_id=run_id,
        )
        self.tools.release_result(result)

    async def _recover(self, session_id: str, records: list[JournalRecord]) -> bool:
        context = await self.context.load(session_id, records)
        states = [
            item
            for record in records
            if record.type == JournalEventType.TOOL_CALL_STATE
            for item in [cast(ToolStateChange, record.payload)]
        ]
        calls = [
            (call.id, record.run_id or "")
            for record in records
            if record.type == JournalEventType.MODEL_RESPONSE_COMMITTED
            for call in cast(ModelCommitted, record.payload).response.calls
        ]
        unknown = bool(self._unresolved_unknown(records))
        for call_id in validate_pairs(context, allow_pending=True):
            latest_state = next(
                (state for state in reversed(states) if state.call_id == call_id), None
            )
            dispatched = next(
                (state.state for state in reversed(states) if state.call_id == call_id), None
            ) in {
                ToolExecutionState.DISPATCHING,
                ToolExecutionState.RUNNING,
                ToolExecutionState.UNKNOWN,
            }
            side_effecting = latest_state is None or latest_state.side_effecting
            await self._result(
                session_id,
                next(run_id for known_call, run_id in reversed(calls) if known_call == call_id),
                ToolResult(
                    call_id=call_id,
                    content=ToolErrorContent(
                        error="Previous execution was interrupted after dispatch; outcome is unknown. Do not replay."
                        if dispatched and side_effecting
                        else "Previous read-only execution was interrupted"
                        if dispatched
                        else "Previous execution ended before dispatch; action was not executed."
                    ),
                    is_error=True,
                    status=ToolStatus.UNKNOWN
                    if dispatched and side_effecting
                    else ToolStatus.CANCELLED,
                ),
            )
            unknown = unknown or dispatched and side_effecting
        stale_handles = await self._running_processes(session_id) - self.tools.owned_commands(
            session_id
        )
        for handle in stale_handles:
            original = next(
                (
                    record
                    for record in reversed(records)
                    if record.type == JournalEventType.TOOL_RESULT_COMMITTED
                    and isinstance(record.payload, ToolResultCommitted)
                    and isinstance(
                        record.payload.result.content, ProcessResult | ToolOutputProjection
                    )
                    and record.payload.result.content.process_handle == handle
                )
            )
            result = cast(ToolResultCommitted, original.payload).result
            await self._settle_process(
                session_id,
                original.run_id,
                result.call_id,
                ProcessResult(
                    process_handle=handle,
                    running=False,
                    side_effect_result_unknown=True,
                    error="Process handle expired after application restart; outcome unknown",
                ),
            )
            unknown = True
        finished = {
            record.run_id for record in records if record.type == JournalEventType.RUN_FINISHED
        }
        for record in records:
            if record.type == JournalEventType.RUN_STARTED and record.run_id not in finished:
                await self.store.append(
                    session_id,
                    JournalEventType.RUN_FINISHED,
                    RunFinished(
                        status=RunStatus.INTERRUPTED,
                        stop_reason=StopReason.UNKNOWN_OUTCOME
                        if unknown
                        else StopReason.PROCESS_INTERRUPTED,
                    ),
                    run_id=record.run_id,
                )
        return unknown

    async def _compact(
        self, environment: RunEnvironment, emit: EventSink, reason: CompactionReason
    ) -> None:
        if self.config.context.strategy != ContextStrategy.SUMMARY:
            raise AgentError(
                ErrorCode.UNSUPPORTED_COMPACTION, "Provider native compaction is unsupported"
            )
        records = await self.store.read(environment.session_id)
        await self.context.session_usage(
            environment.session_id,
            PrefixSnapshot(instructions=environment.instructions, tools=environment.tools),
            records,
        )
        active = await self.context.load(environment.session_id, records)
        validate_pairs(active)
        split = self.context.retained_tail(active, self._unresolved_unknown(records))
        if not split.older.items:
            raise AgentError(
                ErrorCode.CONTEXT_BUDGET,
                "No complete old interaction group is available to compact",
            )
        source_seq = records[-1].seq
        before = self.context.tokens(
            ContextInput(
                instructions=environment.instructions, tools=environment.tools, items=active.items
            )
        )
        await self.store.append(
            environment.session_id,
            JournalEventType.COMPACTION_STARTED,
            CompactionStarted(old_epoch=active.epoch, source_seq=source_seq, reason=reason),
            run_id=environment.run_id or None,
        )
        await self._notify(
            emit, RuntimeEventKind.COMPACTION_STARTED, environment.session_id, environment.run_id
        )
        retained_tokens = self.context.tokens(
            ContextInput(
                instructions=environment.instructions,
                tools=environment.tools,
                items=split.tail.items,
            )
        )
        if retained_tokens >= self.context.compaction_target:
            raise AgentError(
                ErrorCode.CONTEXT_BUDGET,
                "Protected context alone exceeds the target context budget",
            )
        summary = await self._summarize_groups(
            environment,
            ContextInput(
                instructions=environment.instructions, tools=environment.tools, items=active.items
            ),
            split.older,
            split.current_request,
        )
        replacement = ContextWindow(
            items=[
                user_item("Conversation summary (context data):\n" + summary.text).item,
                *split.tail.items,
            ],
            groups=[
                ContextGroup(start=0, end=1, kind=ContextGroupKind.SUMMARY),
                *[
                    ContextGroup(
                        start=group.start + 1,
                        end=group.end + 1,
                        kind=group.kind,
                        complete=group.complete,
                        pending_processes=group.pending_processes,
                    )
                    for group in split.tail.groups
                ],
            ],
        )
        validate_pairs(replacement)
        after = self.context.tokens(
            ContextInput(
                instructions=environment.instructions,
                tools=environment.tools,
                items=replacement.items,
            )
        )
        if after >= before or after > self.context.compaction_target:
            raise AgentError(
                ErrorCode.CONTEXT_BUDGET, "Summary does not meet the target context budget"
            )
        artifact = await self.store.put_artifact(
            environment.session_id, canonical_json(replacement)
        )
        verified = ContextWindow.model_validate_json(
            await self.store.read_artifact(environment.session_id, artifact)
        )
        if canonical_json(verified) != canonical_json(replacement):
            raise AgentError(
                ErrorCode.INVALID_CONTEXT,
                "Persisted checkpoint differs from the replacement window",
            )
        payload = CompactionCommitted(
            epoch=active.epoch + 1,
            source_seq=source_seq,
            artifact_id=artifact,
            strategy=ContextStrategy.SUMMARY,
            before_tokens=before,
            after_tokens=after,
            reason=reason,
        )
        await self.store.append(
            environment.session_id,
            JournalEventType.COMPACTION_COMMITTED,
            payload,
            run_id=environment.run_id or None,
        )
        await self._notify(
            emit,
            RuntimeEventKind.COMPACTION_FINISHED,
            environment.session_id,
            environment.run_id,
            EpochChanged(epoch=active.epoch + 1),
        )

    async def _summarize_groups(
        self,
        environment: RunEnvironment,
        source: ContextInput,
        older: ContextWindow,
        current_request: ContextWindow,
    ) -> ConversationSummary:
        synthesized_input = user_item(SUMMARY_REQUEST).item
        summary: ConversationSummary | None = None
        cursor = 0
        while cursor < len(older.groups):
            accumulated = (
                [user_item("Conversation summary (context data):\n" + summary.text).item]
                if summary is not None
                else []
            )
            count = len(older.groups) - cursor
            while True:
                selected = older.groups[cursor : cursor + count]
                items = older.items[selected[0].start : selected[-1].end]
                request = ModelRequest(
                    model=self.config.model.model,
                    instructions=SUMMARY_INSTRUCTIONS,
                    items=[*accumulated, *items, *current_request.items, synthesized_input],
                    tools=[],
                    cache_key="summary-v3",
                    max_output_tokens=self.config.context.summary_max_output_tokens,
                    reasoning_effort=self.config.summary_effort(),
                )
                summary_input = ContextInput(
                    instructions=request.instructions, tools=request.tools, items=request.items
                )
                summary_tokens = (
                    self.context.summary_tokens(
                        source, summary_input, synthesized_input=synthesized_input
                    )
                    if summary is None
                    else self.context.tokens(summary_input)
                )
                summary_limit = self.config.model.context_window
                if summary_tokens > summary_limit:
                    if count == 1:
                        raise AgentError(
                            ErrorCode.CONTEXT_BUDGET,
                            "A complete context group cannot fit the summary input budget",
                        )
                    count = max(1, count // 2)
                    continue
                try:
                    exchange = await self._response(
                        request, self._discard, environment.session_id, environment.run_id
                    )
                except AgentError as error:
                    if error.code not in {
                        ErrorCode.CONTEXT_OVERFLOW,
                        ErrorCode.CONTEXT_WINDOW_EXCEEDED,
                    }:
                        raise
                    if count == 1:
                        raise AgentError(
                            ErrorCode.CONTEXT_BUDGET,
                            "Provider rejected a complete context group within the summary budget",
                        ) from error
                    count = max(1, count // 2)
                    continue
                if exchange.response.status != ModelResponseStatus.COMPLETED:
                    await self.store.append(
                        environment.session_id,
                        JournalEventType.MODEL_RESPONSE_INCOMPLETE,
                        ModelIncomplete(response=exchange.response),
                        run_id=environment.run_id or None,
                    )
                    raise AgentError(
                        ErrorCode.MODEL_INCOMPLETE,
                        "Context summary response is incomplete; the existing context remains unchanged",
                    )
                if exchange.response.calls:
                    raise AgentError(
                        ErrorCode.INVALID_SUMMARY, "Summary response unexpectedly proposed tools"
                    )
                try:
                    summary = ConversationSummary(text=exchange.response.text)
                except ValidationError as error:
                    raise AgentError(
                        ErrorCode.INVALID_SUMMARY, "Summary must contain nonempty text"
                    ) from error
                cursor += count
                break
        if summary is None:
            raise AgentError(ErrorCode.INVALID_SUMMARY, "No context groups were summarized")
        return summary

    async def _discard(self, event: RuntimeEvent) -> None:
        pass

    async def context_usage(self, session_id: str) -> ContextUsage:
        records = await self.store.read(session_id)
        for record in reversed(records):
            match record.type:
                case JournalEventType.COMPACTION_COMMITTED:
                    break
                case JournalEventType.MODEL_RESPONSE_COMMITTED:
                    return self.context.reported_usage(
                        cast(ModelCommitted, record.payload).response
                    )
        return self.context.reported_usage()

    async def _environment(self, session_id: str, run_id: str) -> RunEnvironment:
        session = await self.store.get_session(session_id)
        workspace = Path(session.workspace)
        instructions = BASE_INSTRUCTIONS + "\n" + await self.tools.instructions(workspace)
        tools = sorted(self.tools.specs(), key=lambda item: item.name)
        revision = prefix_revision(PrefixSnapshot(instructions=instructions, tools=tools))
        return RunEnvironment(
            session_id=session_id,
            run_id=run_id,
            workspace=workspace,
            instructions=instructions,
            tools=tools,
            prefix_revision=revision,
        )

    async def compact(self, session_id: str, emit: EventSink) -> None:
        async with self.store.session_lock(session_id):
            records = await self.store.recover(session_id)
            self.validate_provider(records)
            environment = await self._environment(session_id, "")
            async with asyncio.timeout(self.config.runtime.deadline_seconds):
                await self._compact(environment, emit, CompactionReason.MANUAL)

    async def run(
        self,
        session_id: str,
        prompt: str,
        emit: EventSink,
        approve: ApprovalHandler | None = None,
        command_id: str | None = None,
    ) -> RunResult:
        async with self.store.session_lock(session_id):
            try:
                return await self._run(session_id, prompt, emit, approve, command_id)
            finally:
                self._input_targets = [
                    target for target in self._input_targets if target.session_id != session_id
                ]

    async def _prepare_context(self, environment: RunEnvironment, emit: EventSink) -> ContextWindow:
        records = await self.store.read(environment.session_id)
        await self.context.session_usage(
            environment.session_id,
            PrefixSnapshot(instructions=environment.instructions, tools=environment.tools),
            records,
        )
        active = await self.context.load(environment.session_id, records)
        validate_pairs(active)
        fixed = self.context.tokens(
            ContextInput(instructions=environment.instructions, tools=environment.tools)
        )
        if fixed >= self.context.input_limit:
            raise AgentError(
                ErrorCode.CONTEXT_BUDGET, "Fixed instructions and tools exceed input budget"
            )
        estimated = self.context.tokens(
            ContextInput(
                instructions=environment.instructions, tools=environment.tools, items=active.items
            )
        )
        used = self.context.usage(
            ContextInput(
                instructions=environment.instructions, tools=environment.tools, items=active.items
            )
        ).used_tokens
        if used is None:
            raise ValueError("Prepared context must contain a token estimate")
        if (
            used >= self.config.model.context_window * self.config.context.soft_ratio
            or estimated > self.context.input_limit
        ):
            await self._compact(environment, emit, CompactionReason.AUTOMATIC)
            active = await self.context.load(
                environment.session_id, await self.store.read(environment.session_id)
            )
            estimated = self.context.tokens(
                ContextInput(
                    instructions=environment.instructions,
                    tools=environment.tools,
                    items=active.items,
                )
            )
        if estimated > self.context.input_limit:
            raise AgentError(ErrorCode.CONTEXT_BUDGET, "Input exceeds available context budget")
        return active

    async def _model_step(
        self,
        environment: RunEnvironment,
        active: ContextWindow,
        progress: RunProgress,
        step: int,
        emit: EventSink,
    ) -> ModelExchange:
        while True:
            estimated = self.context.tokens(
                ContextInput(
                    instructions=environment.instructions,
                    tools=environment.tools,
                    items=active.items,
                )
            )
            metadata = ModelRequestMetadata(
                step_id=str(uuid4()),
                step=step,
                context_epoch=active.epoch,
                prefix_revision=environment.prefix_revision,
                context_hash=hashlib.sha256(canonical_json(active).encode()).hexdigest(),
                estimated_input_tokens=estimated,
                cache_key=environment.session_id + ":" + environment.prefix_revision,
            )
            await self.store.append(
                environment.session_id,
                JournalEventType.MODEL_REQUEST_STARTED,
                metadata,
                run_id=environment.run_id,
            )
            await self._notify(
                emit,
                RuntimeEventKind.MODEL_REQUEST_STARTED,
                environment.session_id,
                environment.run_id,
                metadata,
            )
            try:
                exchange = await self._response(
                    ModelRequest(
                        model=self.config.model.model,
                        instructions=environment.instructions,
                        items=active.items,
                        tools=environment.tools,
                        cache_key=metadata.cache_key,
                        max_output_tokens=self.config.model.max_output_tokens,
                        reasoning_effort=self.config.model.reasoning_effort,
                    ),
                    emit,
                    environment.session_id,
                    environment.run_id,
                )
                break
            except AgentError as error:
                await self.store.append(
                    environment.session_id,
                    JournalEventType.MODEL_REQUEST_FAILED,
                    ModelFailure(step_id=metadata.step_id, code=error.code),
                    run_id=environment.run_id,
                )
                if (
                    error.code
                    not in {ErrorCode.CONTEXT_OVERFLOW, ErrorCode.CONTEXT_WINDOW_EXCEEDED}
                    or progress.overflow_retried
                ):
                    raise
                progress.overflow_retried = True
                await self._compact(environment, emit, CompactionReason.PROVIDER_OVERFLOW)
                active = await self.context.load(
                    environment.session_id, await self.store.read(environment.session_id)
                )
        response = exchange.response
        if response.status != ModelResponseStatus.COMPLETED:
            await self.store.append(
                environment.session_id,
                JournalEventType.MODEL_RESPONSE_INCOMPLETE,
                ModelIncomplete(response=response),
                run_id=environment.run_id,
            )
            return exchange
        native = [item for item in response.output if isinstance(item, NativeFunctionCall)]
        if [item.call_id for item in native] != [call.id for call in response.calls]:
            raise AgentError(
                ErrorCode.MODEL_PROTOCOL, "Native calls and parsed tool calls disagree"
            )
        for item, call in zip(native, response.calls, strict=True):
            try:
                arguments = argument_model(call.name).model_validate_json(item.arguments)
            except ValidationError as error:
                raise AgentError(
                    ErrorCode.MODEL_PROTOCOL, "Native tool arguments are invalid JSON"
                ) from error
            if item.name != call.name or arguments != call.arguments:
                raise AgentError(
                    ErrorCode.MODEL_PROTOCOL, "Parsed tool arguments differ from native output"
                )
        validate_pairs(ContextWindow(items=[*active.items, *response.output]), allow_pending=True)
        await self.store.append(
            environment.session_id,
            JournalEventType.MODEL_RESPONSE_COMMITTED,
            ModelCommitted(response=response, metrics=exchange.metrics, step_id=metadata.step_id),
            run_id=environment.run_id,
        )
        completed = ModelCompleted(
            text=response.text,
            reasoning=response.reasoning,
            calls=response.calls,
            step_id=metadata.step_id,
            step=metadata.step,
            context_epoch=metadata.context_epoch,
            prefix_revision=metadata.prefix_revision,
            context_hash=metadata.context_hash,
            estimated_input_tokens=metadata.estimated_input_tokens,
            token_measurement=metadata.token_measurement,
            cache_key=metadata.cache_key,
            duration_seconds=exchange.metrics.duration_seconds,
            ttft_seconds=exchange.metrics.ttft_seconds,
            input_tokens=exchange.metrics.input_tokens,
            output_tokens=exchange.metrics.output_tokens,
            cached_input_tokens=exchange.metrics.cached_input_tokens,
        )
        await self._notify(
            emit,
            RuntimeEventKind.MODEL_COMPLETED,
            environment.session_id,
            environment.run_id,
            completed,
        )
        self.context.usage_after_response(
            ContextInput(
                instructions=environment.instructions, tools=environment.tools, items=active.items
            ),
            response,
        )
        await self._notify(
            emit,
            RuntimeEventKind.CONTEXT_USAGE,
            environment.session_id,
            environment.run_id,
            self.context.reported_usage(response),
        )
        return exchange

    async def _run(
        self,
        session_id: str,
        prompt: str,
        emit: EventSink,
        approve: ApprovalHandler | None,
        command_id: str | None,
    ) -> RunResult:
        records = await self.store.recover(session_id)
        self.validate_provider(records)
        await self._recover(session_id, records)
        records = await self.store.read(session_id)
        if command_id is not None:
            queued = next(
                (
                    cast(PendingInput, record.payload)
                    for record in records
                    if record.type == JournalEventType.PENDING_INPUT
                    and isinstance(record.payload, PendingInput)
                    and record.payload.command_id == command_id
                ),
                None,
            )
            if queued is not None and queued.prompt != prompt:
                raise AgentError(
                    ErrorCode.COMMAND_CONFLICT, "Queued command text differs from submitted text"
                )
            previous = next(
                (
                    record
                    for record in records
                    if record.type == JournalEventType.USER_MESSAGE
                    and cast(UserMessage, record.payload).command_id == command_id
                ),
                None,
            )
            if previous is not None:
                finished = next(
                    (
                        record
                        for record in reversed(records)
                        if record.type == JournalEventType.RUN_FINISHED
                        and record.run_id == previous.run_id
                    ),
                    None,
                )
                if finished is None or previous.run_id is None:
                    raise AgentError(
                        ErrorCode.COMMAND_IN_PROGRESS, "Command has already been accepted"
                    )
                outcome = cast(RunFinished, finished.payload)
                return RunResult(
                    session_id=session_id,
                    run_id=previous.run_id,
                    status=outcome.status,
                    text=outcome.text,
                    stop_reason=outcome.stop_reason,
                )
        environment = await self._environment(session_id, str(uuid4()))
        progress = RunProgress(
            session_id=session_id,
            run_id=environment.run_id,
            status=RunStatus.FAILED,
            stop_reason=StopReason.INTERNAL_ERROR,
        )
        timer = asyncio.timeout(self.config.runtime.deadline_seconds)

        async def tool_emit(event: RuntimeEvent) -> None:
            if event.kind == RuntimeEventKind.TOOL_DISPATCHING:
                dispatch = ToolDispatchEvent.model_validate(event.data)
                for state in [ToolExecutionState.READY, ToolExecutionState.DISPATCHING]:
                    await self.store.append(
                        session_id,
                        JournalEventType.TOOL_CALL_STATE,
                        ToolStateChange(
                            call_id=dispatch.call_id,
                            state=state,
                            side_effecting=dispatch.side_effecting,
                        ),
                        run_id=environment.run_id,
                    )
                if dispatch.side_effecting:
                    progress.dispatched.add(dispatch.call_id)
            await emit(event)

        async def approval(request: ApprovalRequest) -> bool:
            await self.store.append(
                session_id,
                JournalEventType.TOOL_CALL_STATE,
                ToolStateChange(
                    call_id=request.call_id,
                    state=ToolExecutionState.WAITING_APPROVAL,
                    request=request,
                ),
                run_id=environment.run_id,
            )
            loop = asyncio.get_running_loop()
            deadline = timer.when()
            remaining = max(0, deadline - loop.time()) if deadline is not None else None
            timer.reschedule(None)
            try:
                accepted = await approve(request) if approve is not None else False
            finally:
                if remaining is not None:
                    timer.reschedule(loop.time() + remaining)
            await self.store.append(
                session_id,
                JournalEventType.APPROVAL_DECIDED,
                ApprovalDecision(call_id=request.call_id, request_id=request.id, approved=accepted),
                run_id=environment.run_id,
            )
            return accepted

        try:
            await self.store.append(
                session_id,
                JournalEventType.RUN_STARTED,
                RunStarted(
                    provider=self.config.model.provider,
                    base_url=self.config.model.base_url.rstrip("/"),
                    auth_mode=self.config.model.auth_mode,
                    status=RunStatus.RUNNING,
                    model=self.config.model.model,
                    prefix_revision=environment.prefix_revision,
                    instructions=environment.instructions,
                    tools=environment.tools,
                ),
                run_id=environment.run_id,
            )
            message = user_item(prompt)
            message.command_id = command_id
            await self.store.append(
                session_id, JournalEventType.USER_MESSAGE, message, run_id=environment.run_id
            )
            self._input_targets.append(ActiveInputTarget(session_id, environment.run_id))
            async with timer:
                for step in range(self.config.runtime.max_model_steps):
                    await self._consume_steering(environment, emit)
                    active = await self._prepare_context(environment, emit)
                    exchange = await self._model_step(environment, active, progress, step, emit)
                    response = exchange.response
                    progress.text = response.text
                    if response.status != ModelResponseStatus.COMPLETED:
                        progress.status = RunStatus.PARTIAL
                        progress.stop_reason = (
                            StopReason.INCOMPLETE
                            if response.status == ModelResponseStatus.INCOMPLETE
                            else StopReason.FAILED
                        )
                        break
                    if not response.calls:
                        if await self._consume_steering(environment, emit, close_if_empty=True):
                            continue
                        outstanding = await self._running_processes(session_id)
                        progress.status = RunStatus.PARTIAL if outstanding else RunStatus.COMPLETED
                        progress.stop_reason = (
                            StopReason.BACKGROUND_PROCESS_RUNNING
                            if outstanding
                            else StopReason.COMPLETED
                        )
                        if outstanding:
                            progress.text += "\nProcesses are still running: " + ", ".join(
                                sorted(outstanding)
                            )
                        break
                    if (
                        progress.tool_count + len(response.calls)
                        > self.config.runtime.max_tool_calls
                    ):
                        for call in response.calls:
                            await self._result(
                                session_id,
                                environment.run_id,
                                ToolResult(
                                    call_id=call.id,
                                    content=ToolErrorContent(
                                        error="Tool budget exhausted before dispatch"
                                    ),
                                    is_error=True,
                                    status=ToolStatus.CANCELLED,
                                ),
                            )
                        progress.status = RunStatus.PARTIAL
                        progress.stop_reason = StopReason.TOOL_BUDGET
                        break
                    progress.tool_count += len(response.calls)
                    results = await self._tool_batch(
                        environment, response, progress, tool_emit, approval, emit
                    )
                    if any((result.status == ToolStatus.UNKNOWN for result in results)):
                        progress.status = RunStatus.PARTIAL
                        progress.stop_reason = StopReason.UNKNOWN_OUTCOME
                        break
                else:
                    progress.status = RunStatus.PARTIAL
                    progress.stop_reason = StopReason.MODEL_BUDGET
        except asyncio.CancelledError:
            progress.status = RunStatus.CANCELLED
            progress.stop_reason = StopReason.CANCELLED
            await await_owned_completion(self._finish_cancelled(progress, emit))
            raise
        except TimeoutError:
            progress.status = RunStatus.PARTIAL
            progress.stop_reason = StopReason.DEADLINE
            await self._cancel_processes(session_id, environment.run_id)
            await self._finish_pending(progress)
        except AgentError as error:
            progress.status = RunStatus.FAILED
            progress.stop_reason = error.code
            await self._notify(
                emit,
                RuntimeEventKind.ERROR,
                session_id,
                environment.run_id,
                ErrorOccurred(code=error.code, message=error.message),
            )
            await self._finish_pending(progress)
        except Exception:
            await self._finish_pending(progress)
            await self._finish(progress, emit)
            raise
        return await self._finish(progress, emit)

    async def _tool_batch(
        self,
        environment: RunEnvironment,
        response: ModelResponse,
        progress: RunProgress,
        tool_emit: EventSink,
        approve: ApprovalHandler,
        emit: EventSink,
    ) -> list[ToolResult]:
        context = ToolContext(
            approval_mode=self.config.runtime.approval_mode,
            workspace=environment.workspace,
            home=self.store.home,
            session_id=environment.session_id,
            run_id=environment.run_id,
            allow_write=self.config.runtime.allow_write,
            allow_commands=self.config.runtime.allow_commands,
            unresolved_call_ids=sorted(
                self._unresolved_unknown(await self.store.read(environment.session_id))
            ),
        )
        for call in response.calls:
            await self.store.append(
                environment.session_id,
                JournalEventType.TOOL_CALL_STATE,
                ToolStateChange(call_id=call.id, state=ToolExecutionState.PROPOSED, call=call),
                run_id=environment.run_id,
            )

        async def execute(call: ToolCall) -> ToolResult:
            result = await self.tools.execute(call, context, tool_emit, approve)
            if result.call_id != call.id:
                raise AgentError(ErrorCode.TOOL_PROTOCOL, "Tool result call id mismatch")
            await self._result(environment.session_id, environment.run_id, result)
            progress.completed.add(result.call_id)
            await self._notify(
                emit,
                RuntimeEventKind.TOOL_FINISHED,
                environment.session_id,
                environment.run_id,
                ToolFinished(result=result),
            )
            return result

        specs = environment.tools
        results: list[ToolResult] = []
        index = 0
        while index < len(response.calls):
            call = response.calls[index]
            spec = next((spec for spec in specs if spec.name == call.name), None)
            match spec.effect if spec is not None else None:
                case Effect.READ:
                    batch: list[ToolCall] = []
                    while (
                        index < len(response.calls)
                        and len(batch) < self.config.runtime.max_parallel_reads
                    ):
                        candidate = response.calls[index]
                        candidate_spec = next(
                            (spec for spec in specs if spec.name == candidate.name), None
                        )
                        if candidate_spec is None or candidate_spec.effect != Effect.READ:
                            break
                        batch.append(candidate)
                        index += 1
                    async with asyncio.TaskGroup() as group:
                        tasks = [group.create_task(execute(candidate)) for candidate in batch]
                    results.extend((task.result() for task in tasks))
                case _:
                    results.append(await execute(call))
                    if results[-1].status == ToolStatus.UNKNOWN:
                        context.unresolved_call_ids.append(call.id)
                    index += 1
        return results

    async def _finish_pending(self, progress: RunProgress) -> None:
        active = await self.context.load(
            progress.session_id, await self.store.read(progress.session_id)
        )
        for call_id in validate_pairs(active, allow_pending=True):
            if call_id not in progress.completed:
                unknown = call_id in progress.dispatched
                await self._result(
                    progress.session_id,
                    progress.run_id,
                    ToolResult(
                        call_id=call_id,
                        content=ToolErrorContent(
                            error="Execution interrupted; outcome unknown"
                            if unknown
                            else "Cancelled before dispatch"
                        ),
                        is_error=True,
                        status=ToolStatus.UNKNOWN if unknown else ToolStatus.CANCELLED,
                    ),
                )

    async def _finish_cancelled(self, progress: RunProgress, emit: EventSink) -> None:
        await self._cancel_processes(progress.session_id, progress.run_id)
        await self._finish_pending(progress)
        await self._finish(progress, emit)

    async def _finish(self, progress: RunProgress, emit: EventSink) -> RunResult:
        outcome = RunFinished(
            status=progress.status, text=progress.text, stop_reason=progress.stop_reason
        )
        await self.store.append(
            progress.session_id, JournalEventType.RUN_FINISHED, outcome, run_id=progress.run_id
        )
        await self._notify(
            emit, RuntimeEventKind.RUN_FINISHED, progress.session_id, progress.run_id, outcome
        )
        return RunResult(
            session_id=progress.session_id,
            run_id=progress.run_id,
            status=outcome.status,
            text=outcome.text,
            stop_reason=outcome.stop_reason,
        )
