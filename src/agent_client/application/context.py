from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from agent_client.application.prompts import canonical_json
from agent_client.domain.configuration import AppConfig
from agent_client.domain.context_usage import ContextUsage
from agent_client.domain.enums import (
    ErrorCode,
    JournalEventType,
    MessageRole,
    TokenMeasurement,
)
from agent_client.domain.errors import AgentError
from agent_client.domain.events import JournalRecord
from agent_client.domain.models import ModelResponse
from agent_client.domain.protocol import NativeFunctionCall, NativeFunctionOutput, NativeMessage
from agent_client.domain.runtime import (
    BackgroundProcessSettled,
    CompactionCommitted,
    ContextGroup,
    ContextGroupKind,
    ContextInput,
    ContextSplit,
    ContextWindow,
    LegacyContextWindow,
    ModelCommitted,
    NativeUserMessage,
    PrefixSnapshot,
    RunStarted,
    ToolResultCommitted,
    UnknownResolution,
    UserMessage,
)
from agent_client.domain.tools import ToolOutputProjection
from agent_client.domain.workspace import ProcessResult

if TYPE_CHECKING:
    from agent_client.infrastructure.persistence.store import SessionStore


@dataclass(slots=True)
class UsageAnchor:
    request: ContextInput
    usage: ContextUsage


def validate_pairs(window: ContextWindow, *, allow_pending: bool = False) -> list[str]:
    pending: set[str] = set()
    seen: set[str] = set()
    for item in window.items:
        match item:
            case NativeFunctionCall():
                call_id = item.call_id
                if call_id in seen:
                    raise AgentError(ErrorCode.INVALID_CONTEXT, "Missing or duplicate tool call id")
                pending.add(call_id)
                seen.add(call_id)
            case NativeFunctionOutput():
                call_id = item.call_id
                if call_id not in pending:
                    raise AgentError(ErrorCode.INVALID_CONTEXT, "Unpaired or duplicate tool result")
                pending.remove(call_id)
            case _:
                if pending and isinstance(item, NativeMessage) and item.role == MessageRole.USER:
                    raise AgentError(
                        ErrorCode.INVALID_CONTEXT, "User message interrupts a tool batch"
                    )
    if pending and not allow_pending:
        raise AgentError(ErrorCode.INVALID_CONTEXT, "Incomplete tool batch")
    return [
        item.call_id
        for item in window.items
        if isinstance(item, NativeFunctionCall) and item.call_id in pending
    ]


class ContextManager:
    def __init__(self, config: AppConfig, store: SessionStore):
        self.config = config
        self.store = store
        self.usage_anchor: UsageAnchor | None = None

    @property
    def compaction_target(self) -> int:
        return math.floor(self.config.model.context_window * self.config.context.target_ratio)

    @property
    def input_limit(self) -> int:
        model = self.config.model
        return (
            model.context_window
            - model.max_output_tokens
            - max(
                self.config.context.reserve_min_tokens,
                math.ceil(model.context_window * self.config.context.reserve_ratio),
            )
        )

    def tokens(self, request: ContextInput) -> int:
        return cast(int, self.usage(request).used_tokens)

    def usage(self, request: ContextInput) -> ContextUsage:
        anchor = self.usage_anchor
        if anchor is not None and self._extends(anchor.request, request):
            return self.extend_usage(anchor.usage, anchor.request, request)
        return ContextUsage(
            used_tokens=self._estimate_input_tokens(request),
            context_window=self.config.model.context_window,
            input_budget=self.input_limit,
            measurement=TokenMeasurement.UTF8_BYTE_ESTIMATE,
        )

    def _estimate_input_tokens(self, request: ContextInput) -> int:
        return (
            math.ceil(
                len(canonical_json(request).encode("utf-8"))
                / self.config.context.token_bytes_per_token
            )
            + self.config.context.estimate_overhead_tokens
        )

    @staticmethod
    def _extends(previous: ContextInput, current: ContextInput) -> bool:
        return (
            current.instructions == previous.instructions
            and current.tools == previous.tools
            and len(current.items) >= len(previous.items)
            and current.items[: len(previous.items)] == previous.items
        )

    def usage_after_response(self, request: ContextInput, response: ModelResponse) -> ContextUsage:
        input_tokens = response.usage.input_tokens
        output_tokens = response.usage.output_tokens
        updated = request.model_copy()
        updated.items = [*request.items, *response.output]
        if input_tokens is None and output_tokens is None:
            return self.usage(updated)
        if input_tokens is None:
            input_tokens = self._estimate_input_tokens(request)
        if output_tokens is None:
            output_tokens = max(
                0, self._estimate_input_tokens(updated) - self._estimate_input_tokens(request)
            )
        measured = ContextUsage(
            used_tokens=input_tokens + output_tokens,
            context_window=self.config.model.context_window,
            input_budget=self.input_limit,
            measurement=TokenMeasurement.PROVIDER_USAGE_WITH_ESTIMATE,
        )
        self.usage_anchor = UsageAnchor(request=updated, usage=measured)
        return measured

    def reported_usage(self, response: ModelResponse | None = None) -> ContextUsage:
        tokens = response.usage.input_tokens if response is not None else None
        return ContextUsage(
            used_tokens=tokens,
            context_window=self.config.model.context_window,
            input_budget=self.input_limit,
            measurement=TokenMeasurement.PROVIDER_INPUT_TOKENS
            if tokens is not None
            else TokenMeasurement.UNAVAILABLE,
        )

    def extend_usage(
        self, usage: ContextUsage, previous: ContextInput, current: ContextInput
    ) -> ContextUsage:
        if (
            current.instructions != previous.instructions
            or current.tools != previous.tools
            or current.items[: len(previous.items)] != previous.items
            or len(current.items) < len(previous.items)
        ):
            raise ValueError("Context usage extension requires an unchanged prefix")
        if (
            usage.context_window != self.config.model.context_window
            or usage.input_budget != self.input_limit
        ):
            raise ValueError("Context usage belongs to another model budget")
        if usage.used_tokens is None:
            return self.usage(current)
        updated = usage.model_copy()
        updated.used_tokens = usage.used_tokens + max(
            0, self._estimate_input_tokens(current) - self._estimate_input_tokens(previous)
        )
        return updated

    def summary_tokens(
        self,
        source: ContextInput,
        request: ContextInput,
        synthesized_input: NativeUserMessage | None = None,
    ) -> int:
        history = request.items
        if synthesized_input is not None:
            if not history or history[-1] != synthesized_input:
                raise ValueError("Summary instruction must be the final input item")
            history = history[:-1]
        remaining = source.items.copy()
        for item in history:
            if item not in remaining:
                raise ValueError("Summary input must contain only existing context items")
            remaining.remove(item)
        raw = self.tokens(request)
        measured = self.usage(source)
        if measured.measurement != TokenMeasurement.PROVIDER_USAGE_WITH_ESTIMATE:
            return raw
        previous_prefix = ContextInput(instructions=source.instructions, tools=source.tools)
        summary_prefix = ContextInput(
            instructions=request.instructions,
            tools=request.tools,
            items=[synthesized_input] if synthesized_input is not None else [],
        )
        prefix_growth = max(
            0,
            self._estimate_input_tokens(summary_prefix)
            - self._estimate_input_tokens(previous_prefix),
        )
        return min(raw, cast(int, measured.used_tokens) + prefix_growth)

    async def session_usage(
        self, session_id: str, prefix: PrefixSnapshot, records: list[JournalRecord]
    ) -> ContextUsage:
        window = await self.load(session_id, records)
        current = ContextInput(
            instructions=prefix.instructions, tools=prefix.tools, items=window.items
        )
        boundary = -1
        started: RunStarted | None = None
        committed_index: int | None = None
        committed_prefix: RunStarted | None = None
        for index, record in enumerate(records):
            match record.type:
                case JournalEventType.RUN_STARTED:
                    started = cast(RunStarted, record.payload)
                case JournalEventType.COMPACTION_COMMITTED:
                    boundary = index
                case JournalEventType.MODEL_RESPONSE_COMMITTED:
                    committed_index = index
                    committed_prefix = started
        if committed_index is None or committed_index <= boundary or committed_prefix is None:
            return self.usage(current)
        committed = cast(ModelCommitted, records[committed_index].payload)
        response = committed.response
        if response.usage.input_tokens is None and response.usage.output_tokens is None:
            return self.usage(current)
        before_window = await self.load(session_id, records[:committed_index])
        after_window = await self.load(session_id, records[: committed_index + 1])
        request = ContextInput(
            instructions=committed_prefix.instructions,
            tools=committed_prefix.tools,
            items=before_window.items,
        )
        after = ContextInput(
            instructions=committed_prefix.instructions,
            tools=committed_prefix.tools,
            items=after_window.items,
        )
        anchored = self.usage_after_response(request, response)
        if self._extends(after, current):
            return self.extend_usage(anchored, after, current)
        if (
            len(current.items) < len(after.items)
            or current.items[: len(after.items)] != after.items
        ):
            self.usage_anchor = None
            return self.usage(current)
        if anchored.used_tokens is None:
            raise ValueError("A provider anchor must contain token usage")
        used = (
            anchored.used_tokens
            + self._estimate_input_tokens(current)
            - self._estimate_input_tokens(after)
        )
        if used < 0:
            self.usage_anchor = None
            return self.usage(current)
        rebased = ContextUsage(
            used_tokens=used,
            context_window=self.config.model.context_window,
            input_budget=self.input_limit,
            measurement=TokenMeasurement.PROVIDER_USAGE_WITH_ESTIMATE,
        )
        self.usage_anchor = UsageAnchor(request=current, usage=rebased)
        return rebased

    async def load(self, session_id: str, records: list[JournalRecord]) -> ContextWindow:
        context = ContextWindow()
        pending: list[str] = []
        outputs: list[ToolResultCommitted] = []
        current_step: ContextGroup | None = None
        running_handles: set[str] = set()
        call_handles: list[tuple[str, str]] = []

        def flush_outputs() -> None:
            nonlocal pending, current_step, outputs
            complete = all(
                any(result.result.call_id == call_id for result in outputs) for call_id in pending
            )
            context.items.extend(
                result.item
                for call_id in pending
                for result in outputs
                if result.result.call_id == call_id
            )
            if current_step is not None:
                current_step.end = len(context.items)
                current_step.complete = complete
                current_step = None
            pending = []
            outputs = []

        for record in records:
            match record.type:
                case JournalEventType.COMPACTION_COMMITTED:
                    committed = cast(CompactionCommitted, record.payload)
                    raw = await self.store.read_artifact(session_id, committed.artifact_id)
                    legacy = LegacyContextWindow.model_validate_json(raw)
                    context = ContextWindow(
                        items=legacy.items,
                        epoch=legacy.epoch,
                        source_seq=legacy.source_seq,
                        groups=legacy.groups,
                    )
                    validate_pairs(context)
                    context.epoch = committed.epoch
                    context.source_seq = committed.source_seq
                    if not context.groups:
                        context.groups = self._legacy_groups(context)
                    for group in context.groups:
                        for item in context.items[group.start : group.end]:
                            if isinstance(item, NativeFunctionCall):
                                handle = next(
                                    (
                                        known_handle
                                        for known_call, known_handle in call_handles
                                        if known_call == item.call_id
                                    ),
                                    None,
                                )
                                if handle in running_handles:
                                    group.pending_processes.add(handle)
                    pending = []
                    outputs = []
                    current_step = None
                case JournalEventType.USER_MESSAGE:
                    flush_outputs()
                    start = len(context.items)
                    context.items.append(cast(UserMessage, record.payload).item)
                    context.groups.append(
                        ContextGroup(start=start, end=start + 1, kind=ContextGroupKind.USER_REQUEST)
                    )
                case JournalEventType.UNKNOWN_OUTCOME_RESOLVED:
                    flush_outputs()
                    start = len(context.items)
                    context.items.append(cast(UnknownResolution, record.payload).item)
                    context.groups.append(
                        ContextGroup(start=start, end=start + 1, kind=ContextGroupKind.FACT)
                    )
                case JournalEventType.BACKGROUND_PROCESS_SETTLED:
                    flush_outputs()
                    start = len(context.items)
                    settled = cast(BackgroundProcessSettled, record.payload)
                    context.items.append(settled.item)
                    running_handles.discard(settled.process_handle)
                    context.groups.append(
                        ContextGroup(start=start, end=start + 1, kind=ContextGroupKind.FACT)
                    )
                case JournalEventType.MODEL_RESPONSE_COMMITTED:
                    flush_outputs()
                    response = cast(ModelCommitted, record.payload).response
                    start = len(context.items)
                    context.items.extend(response.output)
                    pending = [
                        item.call_id
                        for item in response.output
                        if isinstance(item, NativeFunctionCall)
                    ]
                    if response.output:
                        current_step = ContextGroup(
                            start=start,
                            end=len(context.items),
                            kind=ContextGroupKind.MODEL_STEP,
                            complete=not pending,
                        )
                        context.groups.append(current_step)
                case JournalEventType.TOOL_RESULT_COMMITTED:
                    result = cast(ToolResultCommitted, record.payload)
                    call_id = result.result.call_id
                    if call_id not in pending or any(
                        output.result.call_id == call_id for output in outputs
                    ):
                        raise AgentError(
                            ErrorCode.INVALID_CONTEXT, "Unpaired or duplicate committed tool result"
                        )
                    outputs.append(result)
                    content = result.result.content
                    if (
                        isinstance(content, ProcessResult | ToolOutputProjection)
                        and content.process_handle is not None
                    ):
                        handle = content.process_handle
                        match content.running:
                            case True:
                                running_handles.add(handle)
                                call_handles.append((call_id, handle))
                                if current_step is not None:
                                    current_step.pending_processes.add(handle)
                            case False:
                                running_handles.discard(handle)
                case _:
                    pass
        flush_outputs()
        for group in context.groups:
            group.pending_processes.intersection_update(running_handles)
        context.validate_partition()
        validate_pairs(context, allow_pending=True)
        return context

    def _legacy_groups(self, window: ContextWindow) -> list[ContextGroup]:
        if not window.items:
            return []
        starts = [
            index
            for index, item in enumerate(window.items)
            if isinstance(item, NativeMessage) and item.role == MessageRole.USER
        ]
        if not starts or starts[0] != 0:
            starts.insert(0, 0)
        ends = [*starts[1:], len(window.items)]
        return [
            ContextGroup(
                start=start,
                end=end,
                kind=ContextGroupKind.USER_INTERACTION,
                complete=not validate_pairs(
                    ContextWindow(items=window.items[start:end]), allow_pending=True
                ),
            )
            for start, end in zip(starts, ends, strict=True)
        ]

    def _select_groups(self, window: ContextWindow, selected: list[int]) -> ContextWindow:
        items = []
        groups = []
        for index in selected:
            original = window.groups[index]
            start = len(items)
            items.extend(window.items[original.start : original.end])
            groups.append(
                ContextGroup(
                    start=start,
                    end=len(items),
                    kind=original.kind,
                    complete=original.complete,
                    pending_processes=original.pending_processes,
                )
            )
        return ContextWindow(items=items, groups=groups)

    def retained_tail(
        self, window: ContextWindow, unresolved_call_ids: set[str] | None = None
    ) -> ContextSplit:
        if not window.groups:
            window = ContextWindow(
                items=window.items,
                groups=self._legacy_groups(window),
                epoch=window.epoch,
                source_seq=window.source_seq,
            )
        window.validate_partition()
        requests = [
            index
            for index, group in enumerate(window.groups)
            if group.kind in {ContextGroupKind.USER_REQUEST, ContextGroupKind.USER_INTERACTION}
            and isinstance(window.items[group.start], NativeMessage)
            and window.items[group.start].role == MessageRole.USER
        ]
        if not requests:
            raise AgentError(
                ErrorCode.INVALID_CONTEXT, "Compaction requires a current user request"
            )
        request_index = requests[-1]
        steps = [
            index
            for index, group in enumerate(window.groups)
            if group.kind == ContextGroupKind.MODEL_STEP and group.complete
        ]
        recent = steps[-1] if steps else len(window.groups)
        protected = {request_index, *range(recent, len(window.groups))}
        protected.update(
            index
            for index, group in enumerate(window.groups)
            if not group.complete or group.pending_processes
        )
        if unresolved_call_ids:
            protected.update(
                index
                for index, group in enumerate(window.groups)
                if any(
                    isinstance(item, NativeFunctionCall | NativeFunctionOutput)
                    and item.call_id in unresolved_call_ids
                    for item in window.items[group.start : group.end]
                )
            )
        older = [
            index
            for index, group in enumerate(window.groups)
            if index not in protected and group.complete
        ]
        tail = [index for index in range(len(window.groups)) if index in protected]
        return ContextSplit(
            older=self._select_groups(window, older),
            current_request=self._select_groups(window, [request_index]),
            tail=self._select_groups(window, tail),
        )
