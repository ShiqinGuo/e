import asyncio
import sys
from collections import deque
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import cast

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Footer, Header, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from agent_client.bootstrap import ClientServices
from agent_client.domain.context_usage import ContextUsage
from agent_client.domain.enums import (
    ApprovalMode,
    AuthMode,
    ChatReasoningMode,
    ErrorCode,
    JournalEventType,
    ProviderKind,
    ReasoningEffort,
    RunStatus,
    RuntimeEventKind,
    ToolExecutionState,
    ToolStatus,
)
from agent_client.domain.errors import AgentError
from agent_client.domain.events import RuntimeEvent
from agent_client.domain.mcp import McpConnectionStatus
from agent_client.domain.models import ApprovalRequest, ReasoningBlock
from agent_client.domain.presentation import (
    ApplicationLabel,
    CLICommand,
    DisplayStatus,
    LoginIntent,
    McpStatusView,
    SlashCommand,
    WidgetID,
)
from agent_client.domain.runtime import (
    ContinuationAction,
    ErrorOccurred,
    InputDisposition,
    ModelCommitted,
    ModelCompleted,
    ModelIncomplete,
    ModelMetrics,
    ModelRequestMetadata,
    PendingInput,
    RunFinished,
    TextDelta,
    ToolFinished,
    ToolResultCommitted,
    ToolStateChange,
    UserMessage,
)
from agent_client.domain.tools import ToolDispatchEvent, ToolOutputEvent
from agent_client.domain.transcript import NoticeTone
from agent_client.presentation.clipboard import (
    ClipboardAccess,
    ClipboardAction,
    ClipboardKey,
    ClipboardNotice,
)
from agent_client.presentation.composer import CommandMenu, PromptInput
from agent_client.presentation.status import ContextMeter, WorkIndicator
from agent_client.presentation.transcript import AssistantResponse, TaskTurn, input_summary


@dataclass(frozen=True)
class TranscriptLimits:
    widgets: int = 100
    recent_tasks: int = 20
    message_characters: int = 32000


@dataclass(frozen=True)
class RunTurn:
    run_id: str
    turn: TaskTurn


class AgentApp(App):
    TITLE = ApplicationLabel.STARTUP.value
    BINDINGS = [
        Binding(ClipboardKey.COPY, ClipboardAction.COPY_SELECTION, "Copy", priority=True),
        Binding(
            ClipboardKey.COPY_ALTERNATE,
            ClipboardAction.COPY_SELECTION,
            "Copy",
            priority=True,
            show=False,
        ),
        Binding("escape", "dismiss_or_cancel", "Close menu / cancel", priority=True),
        Binding("ctrl+q", "quit", "Quit", priority=True),
    ]
    CSS = """
    #account { height: 1; padding: 0 1; color: $text-muted; }
    #usage { display: none; }
    #transcript { height: 1fr; padding: 0 1; }
    #composer { height: 1; max-height: 8; border: none; padding: 0 1; }
    #approval { height: auto; max-height: 12; padding: 0 1; }
    #approval-details { height: auto; max-height: 7; }
    #approval-description { height: auto; color: $warning; }
    #approval-choices, #permissions, #reasoning-levels { height: auto; max-height: 8; border: none; padding: 0; }
    #run-status { height: 1; }
    .message { height: auto; margin-bottom: 1; color: $text-muted; }
    """

    def __init__(self, services: ClientServices, workspace: Path, session_id: str | None = None):
        driver_class = None
        if sys.platform == "win32":
            from agent_client.presentation.windows_input import WindowsInputDriver

            driver_class = WindowsInputDriver
        super().__init__(driver_class=driver_class)
        self.services = services
        self.workspace = workspace
        self.session_id = session_id
        self.pending: deque[PendingInput] = deque()
        self.active_run: asyncio.Task | None = None
        self.active_run_id: str | None = None
        self.processing = False
        self.command_busy = False
        self.command_task: asyncio.Task | None = None
        self.stream_text = ""
        self.stream_widget: AssistantResponse | None = None
        self.dirty = False
        self.ready = asyncio.Event()
        self.submit_lock = asyncio.Lock()
        self.turn_lock = asyncio.Lock()
        self.metrics: ModelMetrics | None = None
        self.context_usage = ContextUsage(
            context_window=services.config.model.context_window,
            input_budget=services.runtime.context.input_limit,
        )
        self.command_menu = CommandMenu()
        self.clipboard_access = ClipboardAccess()
        self.turns: list[TaskTurn] = []
        self.run_turns: list[RunTurn] = []
        self.active_turn: TaskTurn | None = None
        self.turn_number = 0
        self.transcript_limits = TranscriptLimits()
        self.activity = ""
        self.approval_future: asyncio.Future[bool] | None = None
        self.approval_lock = asyncio.Lock()

    async def action_copy_selection(self):
        composer = self.query_one(PromptInput)
        text = (
            composer.selected_text
            if composer.has_focus and composer.selected_text
            else self.screen.get_selected_text()
        )
        if not text:
            return
        try:
            await self.clipboard_access.write(self, text)
        except (OSError, UnicodeError, ValueError) as error:
            self.notify(f"Clipboard copy failed: {error}", severity=ClipboardNotice.ERROR)

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Loading...", id=WidgetID.ACCOUNT.value, markup=False)
        yield Static(
            "Usage unavailable | /status shows logs", id=WidgetID.USAGE.value, markup=False
        )
        yield VerticalScroll(id=WidgetID.TRANSCRIPT.value)
        yield WorkIndicator()
        yield ContextMeter(self.context_usage)
        with Vertical(id=WidgetID.APPROVAL.value):
            with VerticalScroll(id=WidgetID.APPROVAL_DETAILS.value):
                yield Static("", id=WidgetID.APPROVAL_DESCRIPTION.value, markup=False)
            yield OptionList(
                Option("Deny", id=WidgetID.DENY.value),
                Option("Allow once", id=WidgetID.ALLOW.value),
                id=WidgetID.APPROVAL_CHOICES.value,
                markup=False,
                compact=True,
            )
        yield OptionList(
            Option("Ask before changes and commands", id=ApprovalMode.ASK.value),
            Option(
                "Never ask · allow changes, commands and MCP tools", id=ApprovalMode.NEVER.value
            ),
            Option(
                "Read only · deny changes, commands and MCP tools", id=ApprovalMode.READ_ONLY.value
            ),
            id=WidgetID.PERMISSIONS.value,
            markup=False,
            compact=True,
        )
        yield OptionList(id=WidgetID.REASONING_LEVELS.value, markup=False, compact=True)
        yield self.command_menu
        yield PromptInput(self.command_menu, self.clipboard_access)
        yield Footer()

    @work
    async def on_mount(self):
        self.query_one(WidgetID.TRANSCRIPT.selector, VerticalScroll).anchor()
        self.query_one(WidgetID.APPROVAL.selector).display = False
        self.query_one(WidgetID.PERMISSIONS.selector).display = False
        self.query_one(WidgetID.REASONING_LEVELS.selector).display = False
        try:
            if self.session_id:
                await self.restore(self.session_id)
            else:
                self.session_id = await self.services.store.create_session(self.workspace)
                await self.refresh_context()
            await self.refresh_account()
            self.set_activity("Idle")
            await self.add_message(
                "Enter a task; /status shows state, /login signs in, /sessions lists history."
            )
            if self.services.config.mcp.servers:
                connected = [
                    status.name
                    for status in self.services.tools.mcp.status
                    if status.status == McpConnectionStatus.CONNECTED
                ]
                unavailable = [
                    f"{status.name}: {status.error or status.status.value}"
                    for status in self.services.tools.mcp.status
                    if status.status != McpConnectionStatus.CONNECTED
                ]
                if connected:
                    await self.add_message("MCP connected: " + ", ".join(connected))
                if unavailable:
                    await self.add_message("MCP unavailable: " + "; ".join(unavailable))
            self.query_one(WidgetID.COMPOSER.selector, TextArea).focus()
            self.ready.set()
            if self.pending:
                await self.add_message(
                    f"Restored {len(self.pending)} pending inputs. Use /continue to execute."
                )
        except AgentError as error:
            await self.add_message(f"{error.code}: {error.message}")
            self.ready.set()

    async def refresh_account(self):
        account = await self.services.access.status()
        model = self.services.config.model
        effort = model.reasoning_effort.value
        if model.provider == ProviderKind.OPENAI_CHAT_COMPLETIONS:
            match model.chat_reasoning:
                case ChatReasoningMode.DISABLED:
                    effort = ReasoningEffort.NONE.value
                case ChatReasoningMode.DEFAULT if not model.chat_send_reasoning_effort:
                    effort = "provider default"
        self.query_one(WidgetID.ACCOUNT.selector, Static).update(
            f"{model.model} · {effort} | {account.status} | {self.workspace.name or self.workspace.anchor}"
        )

    def refresh_usage(self):
        if self.metrics is None:
            self.query_one(WidgetID.USAGE.selector, Static).update(
                "Usage unavailable | /status shows logs"
            )
            return
        values = [
            self.metrics.input_tokens,
            self.metrics.output_tokens,
            self.metrics.cached_input_tokens,
        ]
        labels = [str(value) if value is not None else "unavailable" for value in values]
        self.query_one(WidgetID.USAGE.selector, Static).update(
            f"Last response input/output/cache: {' / '.join(labels)}"
        )

    async def refresh_context(self):
        self.show_context(await self.services.runtime.context_usage(self.session_id))

    def show_context(self, usage: ContextUsage):
        self.context_usage = usage
        self.query_one(ContextMeter).show_usage(usage)

    async def add_message(self, text: str, *, tool: bool = False):
        transcript = self.query_one(WidgetID.TRANSCRIPT.selector, VerticalScroll)
        widget = Static(
            text[: self.transcript_limits.message_characters],
            markup=False,
            classes="tool" if tool else "message",
        )
        await transcript.mount(widget)
        await self.trim_transcript()

    async def trim_transcript(self):
        transcript = self.query_one(WidgetID.TRANSCRIPT.selector, VerticalScroll)
        for widget in list(transcript.children)[: -self.transcript_limits.widgets]:
            if isinstance(widget, TaskTurn):
                if (
                    widget is self.active_turn
                    or any(item.command_id == widget.command_id for item in self.pending)
                    or any(
                        entry.turn is widget and entry.run_id == self.active_run_id
                        for entry in self.run_turns
                    )
                ):
                    continue
                self.turns.remove(widget)
                self.run_turns = [item for item in self.run_turns if item.turn is not widget]
            await widget.remove()

    async def ensure_turn(self, item: PendingInput) -> TaskTurn:
        async with self.turn_lock:
            existing = next(
                (turn for turn in self.turns if turn.command_id == item.command_id), None
            )
            if existing is not None:
                return existing
            self.turn_number += 1
            turn = TaskTurn(item.command_id, item.prompt, self.turn_number)
            transcript = self.query_one(WidgetID.TRANSCRIPT.selector, VerticalScroll)
            await transcript.mount(turn)
            self.turns.append(turn)
            turn.set_status(RunStatus.IDLE, "Steer pending" if item.target_run_id else "Queued")
            if item.continuation_of:
                await turn.note("Continues the previous unfinished task.")
            await self.trim_transcript()
            return turn

    async def clear_transcript(self):
        await self.query_one(WidgetID.TRANSCRIPT.selector, VerticalScroll).remove_children()
        self.turns.clear()
        self.run_turns.clear()
        self.active_turn = None
        self.turn_number = 0
        self.stream_widget = None
        self.stream_text = ""
        self.dirty = False

    async def restore(self, identity: str):
        records = await self.services.store.recover(identity)
        sessions = await self.services.store.list_sessions()
        selected = next((s for s in sessions if s.id == identity), None)
        if selected is None:
            raise AgentError(ErrorCode.SESSION_MISSING, "Session does not exist")
        self.session_id, self.workspace = identity, Path(selected.workspace)
        self.pending = deque(await self.services.runtime.pending_inputs(identity))
        await self.clear_transcript()
        self.metrics = None
        users = [record for record in records if record.type == JournalEventType.USER_MESSAGE]
        selected_runs = {record.run_id for record in users[-self.transcript_limits.recent_tasks :]}
        queued_ids = {item.command_id for item in self.pending}
        selected_commands = {
            cast(UserMessage, record.payload).command_id
            for record in users[-self.transcript_limits.recent_tasks :]
        } | queued_ids
        if len(users) > self.transcript_limits.recent_tasks:
            await self.add_message(
                "Showing the latest 20 tasks. Full history remains in the session journal."
            )
        for record in records:
            if record.type == JournalEventType.PENDING_INPUT:
                item = cast(PendingInput, record.payload)
                if item.command_id in selected_commands:
                    await self.ensure_turn(item)
                continue
            if record.run_id not in selected_runs:
                continue
            turn = next(
                (item.turn for item in reversed(self.run_turns) if item.run_id == record.run_id),
                None,
            )
            match record.type:
                case JournalEventType.USER_MESSAGE:
                    message = cast(UserMessage, record.payload)
                    turn = await self.ensure_turn(
                        PendingInput(
                            command_id=message.command_id or record.event_id,
                            prompt="".join(block.text for block in message.item.content),
                        )
                    )
                    self.run_turns.append(RunTurn(record.run_id, turn))
                    turn.set_status(RunStatus.IDLE, "Restored")
                case JournalEventType.MODEL_REQUEST_STARTED if turn:
                    metadata = cast(ModelRequestMetadata, record.payload)
                    await turn.add_response(metadata.step_id)
                case JournalEventType.MODEL_RESPONSE_COMMITTED if turn:
                    committed = cast(ModelCommitted, record.payload)
                    self.metrics = committed.metrics
                    response = await turn.add_response(committed.step_id or record.event_id)
                    response.set_text(committed.response.text)
                    response.set_reasoning(committed.response.reasoning)
                    response.finish()
                case JournalEventType.MODEL_RESPONSE_INCOMPLETE if turn:
                    incomplete = cast(ModelIncomplete, record.payload)
                    response = next(reversed(turn.responses), None)
                    if response is None:
                        response = await turn.add_response(record.event_id)
                    response.set_text(incomplete.response.text)
                    response.set_reasoning(incomplete.response.reasoning)
                    response.finish()
                    await turn.note("Response interrupted before completion.", NoticeTone.WARNING)
                case JournalEventType.TOOL_CALL_STATE if turn:
                    state = cast(ToolStateChange, record.payload)
                    if state.call:
                        tool = await turn.ensure_tool(state.call_id, state.call.name)
                        tool.set_call(state.call)
                    if state.state == ToolExecutionState.UNKNOWN:
                        await turn.note(
                            f"Unknown tool outcome: {state.call_id}", NoticeTone.WARNING
                        )
                case JournalEventType.TOOL_RESULT_COMMITTED if turn:
                    result = cast(ToolResultCommitted, record.payload)
                    await turn.tool_finished(result.result)
                case JournalEventType.RUN_FINISHED if turn:
                    finished = cast(RunFinished, record.payload)
                    for entry in self.run_turns:
                        if entry.run_id == record.run_id:
                            entry.turn.set_status(finished.status, finished.stop_reason.value)
        for item in self.pending:
            await self.ensure_turn(item)
        for turn in self.turns:
            for response in turn.responses:
                response.finish()
        self.stream_widget = None
        self.stream_text = ""
        self.refresh_usage()
        await self.refresh_context()

    def on_prompt_input_submitted(self, event: PromptInput.Submitted):
        event.stop()
        self.action_submit(queue=event.queue)

    def on_text_area_changed(self, event: TextArea.Changed):
        self.command_menu.update_query(event.text_area.text)
        self.query_one(PromptInput).resize_to_content()

    async def on_option_list_option_selected(self, event: OptionList.OptionSelected):
        event.stop()
        match event.option_list.id:
            case WidgetID.APPROVAL_CHOICES:
                if self.approval_future is not None and not self.approval_future.done():
                    self.approval_future.set_result(event.option_id == WidgetID.ALLOW.value)
            case WidgetID.PERMISSIONS:
                self.services.config.runtime.approval_mode = ApprovalMode(event.option_id)
                event.option_list.display = False
                self.query_one(PromptInput).focus()
                self.set_activity("Idle")
            case WidgetID.REASONING_LEVELS:
                try:
                    self.choose_reasoning(ReasoningEffort(event.option_id))
                except ValueError as error:
                    await self.add_message(str(error))
                event.option_list.display = False
                self.query_one(PromptInput).focus()
            case _ if event.option_id is not None:
                self.query_one(WidgetID.COMPOSER.selector, PromptInput).complete_command(
                    SlashCommand(event.option_id)
                )

    @work
    async def action_submit(self, *, queue: bool = False):
        await self.ready.wait()
        async with self.submit_lock:
            await self.submit_input(queue=queue)

    async def submit_input(self, *, queue: bool = False):
        composer = self.query_one(WidgetID.COMPOSER.selector, TextArea)
        text = composer.text.strip()
        if not text or self.command_busy:
            return
        if text.startswith("/"):
            composer.load_text("")
            await self.command(text)
            return
        try:
            target_run_id = None
            if not queue and self.active_run_id is not None:
                submission = await self.services.runtime.steer(
                    self.session_id, text, self.active_run_id
                )
                command_id = submission.command_id
                if submission.disposition == InputDisposition.STEERED:
                    target_run_id = submission.run_id
            else:
                command_id = await self.services.runtime.enqueue(self.session_id, text)
            composer.load_text("")
            item = PendingInput(command_id=command_id, prompt=text, target_run_id=target_run_id)
            turn = await self.ensure_turn(item)
            consumed = any(entry.turn.command_id == command_id for entry in self.run_turns)
            if not consumed and not any(entry.command_id == command_id for entry in self.pending):
                self.pending.append(item)
                if target_run_id:
                    turn.set_status(RunStatus.IDLE, "Steer pending")
            if not self.processing:
                self.process_queue()
            else:
                self.set_activity("Working")
        except AgentError as error:
            await self.add_message(f"{error.code}: {error.message}")

    @work
    async def process_queue(self):
        if self.processing:
            return
        self.processing = True
        try:
            while self.pending:
                item = self.pending.popleft()
                self.stream_text, self.stream_widget = "", None
                self.active_turn = await self.ensure_turn(item)
                self.active_turn.set_status(RunStatus.RUNNING)
                self.metrics = None
                self.refresh_usage()
                self.set_activity("Working")
                self.active_run = asyncio.create_task(
                    self.services.run(
                        self.session_id,
                        item.prompt,
                        partial(self.emit_for_turn, self.active_turn),
                        self.approve,
                        command_id=item.command_id,
                    )
                )
                try:
                    result = await self.active_run
                    await self.flush_stream()
                    self.active_turn.set_status(result.status, result.stop_reason.value)
                    if result.status in {RunStatus.FAILED, RunStatus.PARTIAL}:
                        await self.active_turn.note(
                            "Later inputs remain queued. Resolve the error, then use /continue."
                        )
                        break
                except asyncio.CancelledError:
                    await self.flush_stream(immediate=True)
                    self.active_turn.set_status(RunStatus.CANCELLED)
                    await self.active_turn.note(
                        "Cancelled. Committed progress remains recoverable.", NoticeTone.WARNING
                    )
                    break
                except AgentError as error:
                    self.active_turn.set_status(RunStatus.FAILED)
                    await self.active_turn.note(f"{error.code}: {error.message}", NoticeTone.ERROR)
                    break
                finally:
                    self.active_run = None
                    self.active_run_id = None
                    self.pending = deque(
                        await self.services.runtime.pending_inputs(self.session_id)
                    )
        finally:
            self.processing = False
            self.active_turn = None
            self.active_run_id = None
            self.set_activity("Idle")

    def set_activity(self, text: str, *, working: bool | None = None):
        queue_count = sum(item.target_run_id is None for item in self.pending)
        steer_count = len(self.pending) - queue_count
        queued = f" | {queue_count} queued" if queue_count else ""
        if steer_count:
            queued += f" | {steer_count} steer pending"
        suffix = f"{queued} | permissions: {self.services.config.runtime.approval_mode.value}"
        if self.processing:
            suffix += " | Enter: steer · Tab: queue"
        activity = f"{text}{suffix}"
        active = self.processing if working is None else working
        indicator = self.query_one(WorkIndicator)
        composer = self.query_one(PromptInput)
        composer.run_active = self.processing
        composer.placeholder = (
            "Enter: steer · Tab: queue · Shift+Enter: newline"
            if self.processing
            else "Type a task or /command"
        )
        if activity != self.activity or indicator.working != active:
            self.activity = activity
            indicator.set_activity(text, suffix, working=active)

    def reasoning_levels(self) -> list[ReasoningEffort]:
        return self.services.config.model.available_reasoning_levels()

    def choose_reasoning(self, effort: ReasoningEffort):
        self.services.config.model.select_reasoning(effort)
        self.set_activity(f"Reasoning: {effort.value}")
        self.run_worker(self.refresh_account())

    async def emit_for_turn(self, turn: TaskTurn, event: RuntimeEvent):
        if not turn.is_mounted:
            return
        if event.run_id and not any(item.run_id == event.run_id for item in self.run_turns):
            self.run_turns.append(RunTurn(event.run_id, turn))
        if event.run_id:
            self.active_run_id = event.run_id
        await self.emit(event)

    async def emit(self, event: RuntimeEvent):
        turn = next(
            (item.turn for item in reversed(self.run_turns) if item.run_id == event.run_id), None
        )
        match event.kind:
            case RuntimeEventKind.INPUT_STEERED:
                message = cast(UserMessage, event.data)
                item = PendingInput(
                    command_id=message.command_id,
                    prompt="".join(block.text for block in message.item.content),
                )
                await self.flush_stream()
                self.stream_widget = None
                self.stream_text = ""
                turn = await self.ensure_turn(item)
                self.pending = deque(
                    entry for entry in self.pending if entry.command_id != message.command_id
                )
                self.run_turns.append(RunTurn(event.run_id, turn))
                self.active_turn = turn
                turn.set_status(RunStatus.RUNNING, "Steer consumed")
            case RuntimeEventKind.CONTEXT_USAGE:
                self.show_context(cast(ContextUsage, event.data))
            case RuntimeEventKind.MODEL_REQUEST_STARTED if turn:
                await self.flush_stream()
                metadata = cast(ModelRequestMetadata, event.data)
                self.stream_widget = await turn.add_response(metadata.step_id)
                self.stream_text = ""
                self.set_activity("Waiting for model")
            case RuntimeEventKind.MODEL_COMPLETED:
                self.metrics = cast(ModelCompleted, event.data)
                self.refresh_usage()
                if turn:
                    response = await turn.add_response(self.metrics.step_id)
                    if self.metrics.text is not None:
                        self.stream_text = self.metrics.text
                        response.set_text(self.stream_text, animate=True)
                        self.dirty = False
                    else:
                        await self.flush_stream()
                    response.set_reasoning(self.metrics.reasoning, animate=True)
                    await response.wait_render()
                    response.finish()
                    for call in self.metrics.calls:
                        tool = await turn.ensure_tool(call.id, call.name)
                        tool.set_call(call)
                    self.set_activity("Response received")
            case RuntimeEventKind.TEXT_DELTA if turn:
                delta = cast(TextDelta, event.data)
                if self.stream_widget is None:
                    self.stream_widget = await turn.add_response(event.run_id)
                self.stream_text += delta.text
                self.stream_widget.set_text(self.stream_text, animate=True)
                self.dirty = True
                self.set_activity("Writing")
                return
            case RuntimeEventKind.REASONING_DELTA if turn:
                block = cast(ReasoningBlock, event.data)
                if self.stream_widget is None:
                    self.stream_widget = await turn.add_response(event.run_id)
                self.stream_widget.append_reasoning(block)
                self.stream_widget.flush_reasoning(animate=True)
                self.dirty = True
                self.set_activity("Thinking")
                return
            case RuntimeEventKind.TOOL_DISPATCHING if turn:
                dispatch = cast(ToolDispatchEvent, event.data)
                await turn.tool_started(dispatch)
                self.set_activity(f"Running {dispatch.name}")
            case RuntimeEventKind.TOOL_OUTPUT_CHUNK if turn:
                output = cast(ToolOutputEvent, event.data)
                tool = await turn.ensure_tool(output.call_id, output.call_id)
                tool.append_output(output)
            case RuntimeEventKind.TOOL_FINISHED if turn:
                finished = cast(ToolFinished, event.data)
                await turn.tool_finished(finished.result)
            case RuntimeEventKind.ERROR:
                error = cast(ErrorOccurred, event.data)
                if turn:
                    await turn.note(error.message, NoticeTone.ERROR)
                else:
                    await self.add_message(f"{error.code}: {error.message}")
            case RuntimeEventKind.COMPACTION_STARTED:
                self.set_activity("Summarizing context", working=True)
            case RuntimeEventKind.COMPACTION_FINISHED:
                await self.refresh_context()
                self.set_activity("Context summarized", working=self.processing)
            case RuntimeEventKind.RUN_FINISHED if turn:
                outcome = cast(RunFinished, event.data)
                await self.flush_stream(immediate=outcome.status == RunStatus.CANCELLED)
                await self.refresh_tool_results(event.run_id, turn)
                for entry in self.run_turns:
                    if entry.run_id == event.run_id:
                        entry.turn.set_status(outcome.status, outcome.stop_reason.value)
                await self.refresh_context()

    async def refresh_tool_results(self, run_id: str, turn: TaskTurn):
        for record in await self.services.store.read(self.session_id):
            if record.run_id != run_id:
                continue
            match record.type:
                case JournalEventType.TOOL_CALL_STATE:
                    state = cast(ToolStateChange, record.payload)
                    if state.call:
                        owner = next(
                            (
                                entry.turn
                                for entry in self.run_turns
                                if entry.run_id == run_id
                                and any(tool.call_id == state.call_id for tool in entry.turn.tools)
                            ),
                            turn,
                        )
                        tool = await owner.ensure_tool(state.call_id, state.call.name)
                        tool.set_call(state.call)
                case JournalEventType.TOOL_RESULT_COMMITTED:
                    result = cast(ToolResultCommitted, record.payload)
                    owner = next(
                        (
                            entry.turn
                            for entry in self.run_turns
                            if entry.run_id == run_id
                            and any(
                                tool.call_id == result.result.call_id for tool in entry.turn.tools
                            )
                        ),
                        turn,
                    )
                    await owner.tool_finished(result.result)

    async def flush_stream(self, *, immediate: bool = False):
        response = self.stream_widget
        if response is not None and response.is_mounted:
            self.dirty = False
            response.set_text(self.stream_text, animate=not immediate)
            response.flush_reasoning(animate=not immediate)
            await response.wait_render()

    async def approve(self, request: ApprovalRequest) -> bool:
        async with self.approval_lock:
            self.approval_future = asyncio.get_running_loop().create_future()
            composer = self.query_one(PromptInput)
            self.command_menu.display = False
            self.query_one(WidgetID.PERMISSIONS.selector).display = False
            self.query_one(WidgetID.REASONING_LEVELS.selector).display = False
            details = input_summary(request.arguments)
            self.query_one(WidgetID.APPROVAL_DESCRIPTION.selector, Static).update(
                f"Allow {request.tool}?\n{details}\nUp/Down: choose · Enter: confirm · Esc: deny"
            )
            self.query_one(WidgetID.APPROVAL.selector).display = True
            choices = self.query_one(WidgetID.APPROVAL_CHOICES.selector, OptionList)
            choices.highlighted = 0
            composer.disabled = True
            choices.focus()
            self.set_activity(f"Waiting for approval · {request.tool}", working=False)
            if self.active_turn:
                tool = await self.active_turn.ensure_tool(request.call_id, request.tool)
                tool.set_waiting()
            try:
                return await self.approval_future
            finally:
                self.approval_future = None
                self.query_one(WidgetID.APPROVAL.selector).display = False
                composer.disabled = False
                composer.focus()

    def action_dismiss_or_cancel(self):
        if self.approval_future is not None:
            if not self.approval_future.done():
                self.approval_future.set_result(False)
            return
        permissions = self.query_one(WidgetID.PERMISSIONS.selector)
        if permissions.display:
            permissions.display = False
            self.query_one(PromptInput).focus()
            return
        reasoning = self.query_one(WidgetID.REASONING_LEVELS.selector)
        if reasoning.display:
            reasoning.display = False
            self.query_one(PromptInput).focus()
            return
        if self.command_menu.display:
            self.command_menu.display = False
            return
        self.action_cancel_run()

    def action_cancel_run(self):
        if self.active_run is not None:
            self.active_run.cancel()
        elif self.command_task is not None:
            self.command_task.cancel()

    async def action_quit(self):
        if self.command_task is not None:
            self.command_task.cancel()
            await asyncio.gather(self.command_task, return_exceptions=True)
        if self.active_run is not None:
            self.active_run.cancel()
            await asyncio.gather(self.active_run, return_exceptions=True)
        self.exit()

    async def on_unmount(self):
        if self.active_run is not None:
            self.active_run.cancel()
            await asyncio.gather(self.active_run, return_exceptions=True)

    async def command(self, text: str):
        raw_name, _, argument = text.partition(" ")
        argument = argument.strip()
        try:
            name = SlashCommand(raw_name)
        except ValueError:
            await self.add_message(
                "Unknown command. Available: " + " ".join(command.value for command in SlashCommand)
            )
            return
        readonly = {
            SlashCommand.STATUS,
            SlashCommand.SESSIONS,
            SlashCommand.SKILLS,
            SlashCommand.MCP,
            SlashCommand.CONFIG,
        }
        if self.processing and name not in readonly:
            await self.add_message("Wait for the active run or cancel it before this command.")
            return
        self.command_busy = True
        self.command_task = asyncio.current_task()
        try:
            match name:
                case SlashCommand.NEW:
                    self.session_id = await self.services.store.create_session(self.workspace)
                    self.pending.clear()
                    await self.clear_transcript()
                    await self.refresh_account()
                    await self.refresh_context()
                case SlashCommand.RESUME:
                    if not argument:
                        raise AgentError(ErrorCode.COMMAND_INVALID, "Usage: /resume <session_id>")
                    await self.restore(argument)
                    await self.refresh_account()
                    if self.pending:
                        await self.add_message(
                            f"Restored {len(self.pending)} pending inputs. Use /continue to execute."
                        )
                case SlashCommand.CONTINUE:
                    plan = await self.services.runtime.prepare_continuation(self.session_id)
                    match plan.action:
                        case ContinuationAction.NO_TASK:
                            await self.add_message("No task to continue.")
                        case ContinuationAction.QUEUED | ContinuationAction.PREPARED:
                            self.pending = deque(plan.inputs)
                            self.process_queue()
                case SlashCommand.SESSIONS:
                    rows = await self.services.store.list_sessions()
                    await self.add_message(
                        "\n".join(f"{s.id} | {s.status} | {s.workspace}" for s in rows[:100])
                        or "No sessions"
                    )
                case SlashCommand.LOGIN:
                    await self.add_message(
                        "Opening browser: Continue with ChatGPT."
                        if self.services.config.model.auth_mode == AuthMode.CHATGPT
                        else "Verifying the configured API key with the provider."
                    )
                    result = await self.services.access.login(
                        new_account=argument == LoginIntent.NEW.value
                    )
                    await self.add_message(result.model_dump_json(exclude_none=True))
                    await self.refresh_account()
                case SlashCommand.LOGOUT:
                    result = await self.services.access.logout()
                    await self.add_message(result.model_dump_json(exclude_none=True))
                    await self.refresh_account()
                case SlashCommand.MODEL:
                    if argument:
                        self.services.config.model.model = argument
                        await self.refresh_account()
                    await self.add_message(
                        f"Current model: {self.services.config.model.model} (process setting)"
                    )
                case SlashCommand.PERMISSIONS:
                    self.query_one(WidgetID.REASONING_LEVELS.selector).display = False
                    if argument:
                        self.services.config.runtime.approval_mode = ApprovalMode(argument)
                        self.set_activity("Idle")
                    else:
                        choices = self.query_one(WidgetID.PERMISSIONS.selector, OptionList)
                        choices.highlighted = list(ApprovalMode).index(
                            self.services.config.runtime.approval_mode
                        )
                        choices.display = True
                        choices.focus()
                case SlashCommand.REASONING:
                    self.query_one(WidgetID.PERMISSIONS.selector).display = False
                    if argument:
                        self.choose_reasoning(ReasoningEffort(argument))
                    else:
                        choices = self.query_one(WidgetID.REASONING_LEVELS.selector, OptionList)
                        choices.clear_options()
                        levels = self.reasoning_levels()
                        choices.add_options(Option(level.value, id=level.value) for level in levels)
                        choices.highlighted = (
                            levels.index(self.services.config.model.reasoning_effort)
                            if self.services.config.model.reasoning_effort in levels
                            else 0
                        )
                        choices.display = True
                        choices.focus()
                case SlashCommand.SKILLS:
                    if not self.processing:
                        await self.services.tools.skills.scan()
                    await self.add_message(
                        "\n".join(
                            entry.model_dump_json()
                            for entry in self.services.tools.skills.search(argument)[:100]
                        )
                    )
                case SlashCommand.MCP:
                    await self.add_message(
                        McpStatusView(servers=self.services.tools.mcp.status).model_dump_json(
                            indent=2
                        )
                    )
                case SlashCommand.COMPACT:
                    await self.services.compact(self.session_id, self.emit)
                case SlashCommand.RESOLVE:
                    parts = argument.split(" ", 2)
                    if len(parts) != 3:
                        raise AgentError(
                            ErrorCode.COMMAND_INVALID,
                            "Usage: /resolve <call_id> failed|succeeded <evidence>",
                        )
                    await self.services.runtime.resolve_unknown(
                        self.session_id, parts[0], ToolStatus(parts[1]), parts[2]
                    )
                    await self.add_message(
                        "Reconciliation recorded. Submit a task or use /continue."
                    )
                case SlashCommand.STATUS | SlashCommand.CONFIG:
                    result = await self.services.access.status()
                    view = DisplayStatus(
                        approval_mode=self.services.config.runtime.approval_mode,
                        session_id=self.session_id,
                        model=self.services.config.model.model,
                        workspace=self.workspace,
                        pending=len(self.pending),
                        allow_write=self.services.config.runtime.allow_write,
                        allow_commands=self.services.config.runtime.allow_commands,
                        logs=self.services.store.home / "logs" / "runtime.jsonl",
                        usage=self.metrics,
                    )
                    await self.add_message(
                        result.model_dump_json(exclude_none=True) + "\n" + view.model_dump_json()
                    )
                case SlashCommand.REBUILD | SlashCommand.DELETE:
                    operation = (
                        CLICommand.REBUILD if name == SlashCommand.REBUILD else CLICommand.DELETE
                    )
                    target = (
                        f" {argument or self.session_id}" if operation == CLICommand.DELETE else ""
                    )
                    await self.add_message(
                        f'Close all clients, then run: agent-client --home "{self.services.store.home}" {operation.value}{target}'
                    )
        except AgentError as error:
            await self.add_message(f"{error.code}: {error.message}")
        except ValueError:
            await self.add_message("Invalid command arguments")
        finally:
            self.command_busy = False
            self.command_task = None
            if not self.processing:
                self.set_activity("Idle")
