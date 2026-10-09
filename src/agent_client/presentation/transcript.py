import asyncio
import re
from dataclasses import dataclass
from enum import StrEnum
from time import monotonic

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.geometry import Offset
from textual.widgets import Collapsible, Markdown, Static

from agent_client.domain.enums import ReasoningChannel, RunStatus, StopReason, ToolStatus
from agent_client.domain.models import ReasoningBlock, ToolCall, ToolResult
from agent_client.domain.skills import SkillLoadResult, SkillResourceResult
from agent_client.domain.tools import (
    ApplyPatchArguments,
    CommandHandleArguments,
    LoadSkillArguments,
    McpCallArguments,
    McpPageArguments,
    McpPromptArguments,
    McpReadResourceArguments,
    ReadFileArguments,
    ReadOutputArguments,
    RunCommandArguments,
    SearchArguments,
    SearchTextArguments,
    SkillResourceArguments,
    ToolArguments,
    ToolContent,
    ToolDispatchEvent,
    ToolErrorContent,
    ToolName,
    ToolOutputEvent,
    ToolOutputProjection,
    ToolOutputRange,
    ToolPathArguments,
    WriteFileArguments,
)
from agent_client.domain.transcript import NoticeTone
from agent_client.domain.workspace import (
    FileReadResult,
    FileWriteResult,
    ProcessResult,
)
from agent_client.presentation.streaming import FRAME_SECONDS, REVEAL_SECONDS, reveal_prefix


class ToolDisplayStatus(StrEnum):
    QUEUED = "queued"
    WORKING = "working"
    WAITING = "waiting for approval"
    DONE = "done"
    UNKNOWN = "outcome unknown · check before retrying"


class ToolAction(StrEnum):
    READ = "Read file"
    LIST = "List files"
    SEARCH = "Search"
    COMMAND = "Run command"
    EDIT = "Edit file"
    REMOTE = "Remote tool"


class RenderStyle(StrEnum):
    BOLD = "bold"
    BOLD_CYAN = "bold cyan"
    CYAN = "cyan"
    GREEN = "green"
    RED = "red"
    YELLOW = "yellow"
    DIM = "dim"


class ToolOutputField(StrEnum):
    PATH = "path"
    ERROR = "error"
    NOTICE = "notice"
    CONTENT = "content"
    STDOUT = "stdout"
    STDERR = "stderr"
    PREVIEW = "preview"
    TAIL = "tail"
    APPLICABLE_INSTRUCTIONS = "applicable_instructions"
    TIMED_OUT = "timed_out"
    SIDE_EFFECT_RESULT_UNKNOWN = "side_effect_result_unknown"
    TRUNCATED = "truncated"
    FULL_OUTPUT_TRUNCATED = "full_output_truncated"
    ARTIFACT_ID = "artifact_id"
    BEFORE_ARTIFACT_ID = "before_artifact_id"
    AFTER_ARTIFACT_ID = "after_artifact_id"
    STDOUT_ARTIFACT_ID = "stdout_artifact_id"
    STDERR_ARTIFACT_ID = "stderr_artifact_id"
    PROCESS_HANDLE = "process_handle"


@dataclass(frozen=True)
class ToolRenderingLimits:
    input_characters: int = 160
    diff_lines: int = 12
    error_lines: int = 3
    error_characters: int = 500


DEFAULT_TOOL_RENDERING_LIMITS = ToolRenderingLimits()


@dataclass(frozen=True)
class ToolPreview:
    path: str | None = None
    content: str | None = None
    diff: str | None = None
    stdout: str | None = None
    stderr: str | None = None
    error: str | None = None
    notice: str | None = None
    preview: str | None = None
    tail: str | None = None
    artifact_id: str | None = None
    before_artifact_id: str | None = None
    after_artifact_id: str | None = None
    stdout_artifact_id: str | None = None
    stderr_artifact_id: str | None = None
    truncated: bool = False
    full_output_truncated: bool = False
    timed_out: bool = False
    side_effect_result_unknown: bool = False
    exit_code: int | None = None
    process_handle: str | None = None
    applicable_instructions: str | None = None


PREVIEW_LINES = 120
PREVIEW_CHARACTERS = 12000


def bounded_text(value: str) -> str:
    lines = value.splitlines()
    preview = "\n".join(lines[:PREVIEW_LINES])[:PREVIEW_CHARACTERS]
    if len(lines) > PREVIEW_LINES or len(preview) < len(value.rstrip("\n")):
        preview += (
            "\n… Preview truncated; use the recorded tool output or artifact for full details."
        )
    return preview


def render_diff(value: str, line_limit: int = PREVIEW_LINES) -> Text:
    result = Text()
    lines = value.splitlines()
    additions = deletions = 0
    in_hunk = False
    for line in lines:
        match line:
            case hunk if hunk.startswith("@@"):
                in_hunk = True
            case header if header.startswith("diff "):
                in_hunk = False
            case addition if in_hunk and addition.startswith("+"):
                additions += 1
            case deletion if in_hunk and deletion.startswith("-"):
                deletions += 1
    result.append(f"Changes: +{additions} / -{deletions}\n", style=RenderStyle.BOLD)
    old_line = 0
    new_line = 0
    in_hunk = False
    visible_lines = bounded_text(value).splitlines()[:line_limit]
    for line in visible_lines:
        match line:
            case header if header.startswith("diff "):
                in_hunk = False
                style = RenderStyle.BOLD_CYAN
            case header if not in_hunk and header.startswith(("--- ", "+++ ")):
                style = RenderStyle.BOLD_CYAN
            case hunk if hunk.startswith("@@"):
                in_hunk = True
                style = RenderStyle.CYAN
                coordinates = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", hunk)
                if coordinates is not None:
                    old_line, new_line = map(int, coordinates.groups())
            case addition if addition.startswith("+"):
                style = RenderStyle.GREEN
                result.append(f"{'':>5} {new_line:>5} │ ", style=RenderStyle.DIM)
                new_line += 1
            case deletion if deletion.startswith("-"):
                style = RenderStyle.RED
                result.append(f"{old_line:>5} {'':>5} │ ", style=RenderStyle.DIM)
                old_line += 1
            case context if context.startswith(" "):
                style = RenderStyle.DIM
                result.append(f"{old_line:>5} {new_line:>5} │ ", style=RenderStyle.DIM)
                old_line += 1
                new_line += 1
            case _:
                style = RenderStyle.DIM
        result.append(line + "\n", style=style)
    if len(lines) > line_limit:
        result.append("… More changes in details\n", style=RenderStyle.DIM)
    return result


def input_summary(arguments: ToolArguments) -> str:
    match arguments:
        case RunCommandArguments():
            return arguments.command
        case SearchTextArguments() | SearchArguments():
            return arguments.query
        case (
            ReadFileArguments() | WriteFileArguments() | ApplyPatchArguments() | ToolPathArguments()
        ):
            return arguments.path
        case SkillResourceArguments():
            return f"{arguments.skill_id} · {arguments.relative_path}"
        case LoadSkillArguments():
            return arguments.skill_id
        case McpCallArguments():
            return arguments.tool_id
        case CommandHandleArguments():
            return arguments.process_handle
        case ReadOutputArguments():
            return arguments.artifact_id
        case McpReadResourceArguments():
            return f"{arguments.server} · {arguments.uri}"
        case McpPromptArguments():
            return f"{arguments.server} · {arguments.name}"
        case McpPageArguments():
            return arguments.server


def tool_preview(content: ToolContent) -> ToolPreview | None:
    match content:
        case FileReadResult():
            return ToolPreview(
                path=content.path,
                content=content.content,
                truncated=content.truncated,
                applicable_instructions=content.applicable_instructions,
            )
        case FileWriteResult():
            return ToolPreview(
                path=content.path,
                diff=content.diff,
                before_artifact_id=content.before_artifact_id,
                after_artifact_id=content.after_artifact_id,
                applicable_instructions=content.applicable_instructions,
            )
        case ProcessResult():
            return ToolPreview(
                stdout=content.stdout,
                stderr=content.stderr,
                error=content.error,
                notice=content.notice,
                exit_code=content.exit_code,
                timed_out=content.timed_out,
                side_effect_result_unknown=content.side_effect_result_unknown,
                truncated=content.truncated,
                full_output_truncated=content.full_output_truncated,
                stdout_artifact_id=content.stdout_artifact_id,
                stderr_artifact_id=content.stderr_artifact_id,
                process_handle=content.process_handle,
                applicable_instructions=content.applicable_instructions,
            )
        case ToolErrorContent():
            return ToolPreview(
                error=content.error, applicable_instructions=content.applicable_instructions
            )
        case ToolOutputProjection():
            return ToolPreview(
                preview=content.preview,
                tail=content.tail,
                artifact_id=content.artifact_id,
                exit_code=content.exit_code,
                timed_out=content.timed_out,
                truncated=content.truncated,
                full_output_truncated=content.full_output_truncated,
                stdout_artifact_id=content.stdout_artifact_id,
                stderr_artifact_id=content.stderr_artifact_id,
                process_handle=content.process_handle,
                applicable_instructions=content.applicable_instructions,
            )
        case SkillLoadResult() | SkillResourceResult() | ToolOutputRange():
            return ToolPreview(content=content.content)
        case _:
            return None


class AssistantResponse(Vertical):
    DEFAULT_CSS = """
    AssistantResponse { height: auto; margin: 1 0; padding: 0; }
    AssistantResponse .response-body { height: auto; }
    AssistantResponse .response-marker { width: 2; height: 1; color: $text; }
    AssistantResponse .reasoning { height: auto; color: #a8adb5; padding: 0 2; margin: 0 0 1 0; }
    AssistantResponse Markdown { width: 1fr; height: auto; margin: 0; color: $text; }
    """

    def __init__(self, step_id: str, number: int) -> None:
        super().__init__()
        self.step_id = step_id
        self.number = number
        self.text = ""
        self.heading = Static("•", classes="response-marker", markup=False)
        self.markdown = Markdown("")
        self.body = Horizontal(self.heading, self.markdown, classes="response-body")
        self.body.display = False
        self.reasoning_view = Static("", classes="reasoning", markup=False)
        self.reasoning_view.display = False
        self.reasoning: list[ReasoningBlock] = []
        self.rendered_text = ""
        self.rendered_reasoning = ""
        self.reasoning_target = ""
        self.animate_text = False
        self.animate_reasoning = False
        self.render_deadline = 0.0
        self.render_seconds = FRAME_SECONDS
        self.render_task: asyncio.Task | None = None

    def compose(self) -> ComposeResult:
        yield self.reasoning_view
        yield self.body

    def set_text(self, text: str, *, animate: bool = False) -> None:
        self.text = text
        self.animate_text = animate
        self.body.display = bool(text.strip())
        self.start_render()

    def start_render(self):
        if (
            self.rendered_text != self.text or self.rendered_reasoning != self.reasoning_target
        ) and (self.render_task is None or self.render_task.done()):
            self.render_deadline = monotonic() + REVEAL_SECONDS
            self.render_task = asyncio.create_task(self.render_pending())

    async def render_pending(self):
        while self.rendered_text != self.text or self.rendered_reasoning != self.reasoning_target:
            started = monotonic()
            remaining = self.render_deadline - started
            target = reveal_prefix(
                self.text,
                self.rendered_text,
                animate=self.animate_text,
                remaining_seconds=remaining,
                render_seconds=self.render_seconds,
            )
            reasoning = reveal_prefix(
                self.reasoning_target,
                self.rendered_reasoning,
                animate=self.animate_reasoning,
                remaining_seconds=remaining,
                render_seconds=self.render_seconds,
            )
            if reasoning != self.rendered_reasoning:
                self.reasoning_view.update(reasoning)
                self.rendered_reasoning = reasoning
                self.reasoning_view.display = bool(reasoning.strip())
            if target != self.rendered_text:
                if target.startswith(self.rendered_text):
                    await self.markdown.append(target[len(self.rendered_text) :])
                else:
                    await self.markdown.update(target)
                self.rendered_text = target
            duration = monotonic() - started
            self.render_seconds = max(FRAME_SECONDS, duration)
            if self.rendered_text != self.text or self.rendered_reasoning != self.reasoning_target:
                animated = (self.animate_text and self.rendered_text != self.text) or (
                    self.animate_reasoning and self.rendered_reasoning != self.reasoning_target
                )
                if animated:
                    await asyncio.sleep(max(0, FRAME_SECONDS - duration))

    async def wait_render(self):
        if self.render_task is not None:
            await asyncio.shield(self.render_task)

    def append_reasoning(self, block: ReasoningBlock):
        existing = next(
            (
                item
                for item in self.reasoning
                if (item.item_id, item.index, item.channel)
                == (block.item_id, block.index, block.channel)
            ),
            None,
        )
        if existing is None:
            self.reasoning.append(block.model_copy())
        else:
            existing.text += block.text

    def set_reasoning(self, blocks: list[ReasoningBlock], *, animate: bool = False):
        self.reasoning = [block.model_copy() for block in blocks]
        self.flush_reasoning(animate=animate)

    def flush_reasoning(self, *, animate: bool = False):
        summarized = {
            block.item_id
            for block in self.reasoning
            if block.channel == ReasoningChannel.SUMMARY and block.text.strip()
        }
        text = "\n\n".join(
            block.text
            for block in self.reasoning
            if block.channel == ReasoningChannel.SUMMARY or block.item_id not in summarized
        )
        self.reasoning_target = text
        self.animate_reasoning = animate
        if not animate and self.rendered_reasoning != text:
            self.reasoning_view.update(text)
            self.rendered_reasoning = text
            self.reasoning_view.display = bool(text.strip())
        self.start_render()

    async def on_unmount(self):
        if self.render_task is not None and not self.render_task.done():
            self.render_task.cancel()
            await asyncio.gather(self.render_task, return_exceptions=True)

    def finish(self) -> None:
        self.body.display = bool(self.text.strip())
        self.flush_reasoning()


class ToolBlock(Vertical):
    DEFAULT_CSS = """
    ToolBlock { height: auto; margin: 0 0 1 1; padding: 0 1; border-left: solid $warning; }
    ToolBlock > Static { height: auto; color: $warning; }
    ToolBlock.success { border-left: solid $success; }
    ToolBlock.success > Static { color: $success; }
    ToolBlock.error { border-left: solid $error; }
    ToolBlock.error > Static { color: $error; }
    ToolBlock Collapsible { height: auto; padding: 0; border: none; }
    ToolBlock Collapsible > Contents { padding: 0 0 0 3; }
    ToolBlock Collapsible Static { height: auto; }
    """

    def __init__(self, call_id: str, name: str) -> None:
        super().__init__()
        self.call_id = call_id
        self.tool_name = name
        self.status: ToolStatus | ToolDisplayStatus = ToolDisplayStatus.QUEUED
        self.input_summary = ""
        self.heading = Static(f"● {self.tool_name} · {self.status}")
        self.summary = Static("", markup=False)
        self.summary.display = False
        self.output = Static("", markup=False)
        self.details = Collapsible(self.output, title="Tool output", collapsed=True)
        self.stream = ""
        self.output_press: Offset | None = None
        self.output_dragged = False
        self.update_heading(self.status)

    def compose(self) -> ComposeResult:
        yield self.heading
        yield self.summary
        yield self.details

    def on_mouse_down(self, event: events.MouseDown):
        self.output_press = (
            event.screen_offset
            if event.button == 1
            and self.screen.get_widget_at(*event.screen_offset)[0] is self.output
            else None
        )
        self.output_dragged = False

    def on_mouse_move(self, event: events.MouseMove):
        if self.output_press is not None and event.screen_offset != self.output_press:
            self.output_dragged = True

    def on_click(self, event: events.Click):
        if (
            self.screen.get_widget_at(*event.screen_offset)[0] is self.output
            and event.button == 1
            and not self.details.collapsed
            and event.screen_offset == self.output_press
            and not self.output_dragged
        ):
            event.stop()
            self.details.collapsed = True
            self.call_after_refresh(
                self.details.query_one("CollapsibleTitle").scroll_visible, animate=False
            )

    def set_name(self, name: str) -> None:
        self.tool_name = name
        self.update_heading(self.status)

    def update_heading(self, status: ToolStatus | ToolDisplayStatus) -> None:
        match self.tool_name:
            case ToolName.READ_FILE | ToolName.READ_SKILL_RESOURCE | ToolName.READ_TOOL_OUTPUT:
                action = ToolAction.READ
            case ToolName.LIST_FILES:
                action = ToolAction.LIST
            case ToolName.SEARCH_TEXT | ToolName.SEARCH_SKILLS | ToolName.SEARCH_MCP_TOOLS:
                action = ToolAction.SEARCH
            case ToolName.RUN_COMMAND | ToolName.POLL_COMMAND:
                action = ToolAction.COMMAND
            case ToolName.APPLY_PATCH | ToolName.WRITE_FILE:
                action = ToolAction.EDIT
            case ToolName.CALL_MCP_TOOL:
                action = ToolAction.REMOTE
            case _:
                action = self.tool_name
        match status:
            case ToolStatus.SUCCEEDED:
                status = ToolDisplayStatus.DONE
            case ToolStatus.RUNNING:
                status = ToolDisplayStatus.WORKING
        summary = f" · {self.input_summary}" if self.input_summary else ""
        self.heading.update(Text(f"● {action}{summary} · {status}"))

    def set_call(self, call: ToolCall) -> None:
        self.tool_name = call.name
        self.input_summary = " ".join(input_summary(call.arguments).split())[
            : DEFAULT_TOOL_RENDERING_LIMITS.input_characters
        ]
        self.update_heading(self.status)

    def set_waiting(self) -> None:
        self.update_heading(ToolDisplayStatus.WAITING)

    def start(self) -> None:
        self.status = ToolStatus.RUNNING
        self.update_heading(self.status)

    def append_output(self, event: ToolOutputEvent) -> None:
        label = event.channel.value
        self.stream = bounded_text(self.stream + f"[{label}] {event.text}")
        self.output.update(self.stream)
        self.details.title = "Live output"

    async def finish(self, result: ToolResult) -> None:
        self.status = result.status
        failed = result.is_error or result.status in (ToolStatus.FAILED, ToolStatus.DENIED)
        self.set_class(failed, "error")
        self.set_class(result.status == ToolStatus.SUCCEEDED and not failed, "success")
        status = ToolStatus.FAILED if failed else result.status
        if result.status == ToolStatus.UNKNOWN:
            status = ToolDisplayStatus.UNKNOWN
        self.update_heading(status)
        self.summary.display = False
        preview = tool_preview(result.content)
        if preview is not None:
            if preview.path and not self.input_summary:
                self.input_summary = preview.path[: DEFAULT_TOOL_RENDERING_LIMITS.input_characters]
                self.update_heading(status)
            if preview.diff is not None:
                self.summary.update(
                    render_diff(preview.diff, line_limit=DEFAULT_TOOL_RENDERING_LIMITS.diff_lines)
                )
                self.summary.display = True
            if failed or result.status == ToolStatus.UNKNOWN:
                error = preview.error or preview.stderr or preview.notice or ""
                lines = error.splitlines()
                excerpt = "\n".join(
                    lines[: DEFAULT_TOOL_RENDERING_LIMITS.error_lines]
                    if preview.error
                    else lines[-DEFAULT_TOOL_RENDERING_LIMITS.error_lines :]
                )[: DEFAULT_TOOL_RENDERING_LIMITS.error_characters]
                if excerpt:
                    self.summary.update(
                        Text(excerpt, style=RenderStyle.RED if failed else RenderStyle.YELLOW)
                    )
                    self.summary.display = True
        rendered = Text()
        meaningful = False
        if preview is not None:
            for label, value in (
                (ToolOutputField.PATH, preview.path),
                (ToolOutputField.ERROR, preview.error),
                (ToolOutputField.NOTICE, preview.notice),
                (ToolOutputField.CONTENT, preview.content),
                (ToolOutputField.STDOUT, preview.stdout),
                (ToolOutputField.STDERR, preview.stderr),
                (ToolOutputField.PREVIEW, preview.preview),
                (ToolOutputField.TAIL, preview.tail),
                (ToolOutputField.APPLICABLE_INSTRUCTIONS, preview.applicable_instructions),
            ):
                if value:
                    meaningful = True
                    rendered.append(f"{label}:\n", style=RenderStyle.BOLD)
                    rendered.append(bounded_text(value) + "\n")
            if preview.diff is not None:
                meaningful = True
                rendered.append(render_diff(preview.diff))
            if preview.exit_code is not None:
                rendered.append(f"Exit code: {preview.exit_code}\n")
            for label, enabled in (
                (ToolOutputField.TIMED_OUT, preview.timed_out),
                (ToolOutputField.SIDE_EFFECT_RESULT_UNKNOWN, preview.side_effect_result_unknown),
                (ToolOutputField.TRUNCATED, preview.truncated),
                (ToolOutputField.FULL_OUTPUT_TRUNCATED, preview.full_output_truncated),
            ):
                if enabled:
                    rendered.append(f"{label}: true\n", style=RenderStyle.YELLOW)
            for label, value in (
                (ToolOutputField.ARTIFACT_ID, preview.artifact_id),
                (ToolOutputField.BEFORE_ARTIFACT_ID, preview.before_artifact_id),
                (ToolOutputField.AFTER_ARTIFACT_ID, preview.after_artifact_id),
                (ToolOutputField.STDOUT_ARTIFACT_ID, preview.stdout_artifact_id),
                (ToolOutputField.STDERR_ARTIFACT_ID, preview.stderr_artifact_id),
                (ToolOutputField.PROCESS_HANDLE, preview.process_handle),
            ):
                if value:
                    rendered.append(f"{label}: {value}\n", style=RenderStyle.CYAN)
        if not meaningful:
            rendered.append("Raw protocol details:\n", style=RenderStyle.DIM)
            raw = result.content.model_dump_json(indent=2)
            rendered.append(bounded_text(raw))
        if result.artifact_id:
            rendered.append(f"\nArtifact: {result.artifact_id}", style=RenderStyle.CYAN)
        self.output.update(rendered)
        self.details.title = (
            "Changes" if preview is not None and preview.diff is not None else "Tool output"
        )
        self.details.collapsed = True


class TaskTurn(Vertical):
    DEFAULT_CSS = """
    TaskTurn { height: auto; margin: 1 0; padding: 0; }
    TaskTurn > .user-input { height: auto; background: #16435a 35%; color: #7dd3fc; padding: 0 1; margin: 0 0 1 0; }
    TaskTurn > .notice { height: auto; color: $text-muted; padding: 0 1; }
    TaskTurn > .notice.warning { color: $warning; }
    TaskTurn > .notice.error { color: $error; }
    TaskTurn > .notice.success { color: $success; }
    TaskTurn > .turn-status { height: auto; color: $text-muted; margin: 1 0 0 0; }
    TaskTurn.failed > .turn-status { color: $error; }
    """

    def __init__(self, command_id: str, prompt: str, number: int) -> None:
        super().__init__()
        self.command_id = command_id
        self.prompt = prompt
        self.number = number
        self.started_at: float | None = None
        self.responses: list[AssistantResponse] = []
        self.tools: list[ToolBlock] = []
        self.status_line = Static("", classes="turn-status", markup=False)
        self.status_line.display = False

    def compose(self) -> ComposeResult:
        yield Static(Text(f"› {self.prompt}"), classes="user-input")
        yield self.status_line

    def set_status(self, status: RunStatus, detail: str = "") -> None:
        if status == RunStatus.RUNNING and self.started_at is None:
            self.started_at = monotonic()
        label = detail.lower() if status == RunStatus.IDLE and detail else status.value
        elapsed = f" · {monotonic() - self.started_at:.1f}s" if self.started_at is not None else ""
        match detail:
            case StopReason.MODEL_BUDGET | StopReason.TOOL_BUDGET:
                outcome = "Budget reached · /continue to resume"
            case StopReason.DEADLINE:
                outcome = "Time limit reached · /continue to resume"
            case StopReason.BACKGROUND_PROCESS_RUNNING:
                outcome = "Background command still running"
            case StopReason.UNKNOWN_OUTCOME:
                outcome = "Outcome unknown · verify before retrying"
            case _:
                outcome = label
        self.status_line.update(outcome + elapsed)
        self.status_line.display = status != RunStatus.RUNNING
        for state in (RunStatus.RUNNING, RunStatus.COMPLETED, RunStatus.FAILED):
            self.set_class(status == state, state.value)
        if status not in (RunStatus.IDLE, RunStatus.RUNNING):
            for response in self.responses:
                response.finish()

    async def add_response(self, step_id: str) -> AssistantResponse:
        response = next((item for item in self.responses if item.step_id == step_id), None)
        if response is None:
            response = AssistantResponse(step_id, len(self.responses) + 1)
            self.responses.append(response)
            await self.mount(response, before=self.status_line)
        return response

    async def tool_started(self, dispatch: ToolDispatchEvent) -> ToolBlock:
        tool = await self.ensure_tool(dispatch.call_id, dispatch.name)
        tool.start()
        return tool

    async def ensure_tool(self, call_id: str, name: str) -> ToolBlock:
        tool = next((item for item in self.tools if item.call_id == call_id), None)
        if tool is None:
            tool = ToolBlock(call_id, name)
            self.tools.append(tool)
            await self.mount(tool, before=self.status_line)
        return tool

    async def tool_finished(self, result: ToolResult) -> ToolBlock:
        tool = await self.ensure_tool(result.call_id, result.call_id)
        await tool.finish(result)
        return tool

    async def note(self, text: str, tone: NoticeTone = NoticeTone.INFO) -> None:
        await self.mount(
            Static(Text(text), classes=f"notice {tone.value}"), before=self.status_line
        )
