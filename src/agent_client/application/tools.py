import asyncio
import hashlib
import os
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from filelock import AsyncFileLock
from jsonschema import ValidationError as SchemaValidationError
from pydantic import TypeAdapter

from agent_client.application.instructions import InstructionResolver, scoped_path
from agent_client.application.skills import SkillCatalog
from agent_client.application.tool_output import ToolOutputProjector
from agent_client.domain.configuration import AppConfig
from agent_client.domain.enums import ApprovalMode, RuntimeEventKind, ToolStatus
from agent_client.domain.events import EventSink, RuntimeEvent
from agent_client.domain.mcp import (
    McpOperation,
    McpRequestArguments,
    McpServerDirectory,
    McpToolResult,
)
from agent_client.domain.models import (
    ApprovalHandler,
    ApprovalRequest,
    Contract,
    Effect,
    ToolCall,
    ToolContext,
    ToolResult,
    ToolSpec,
)
from agent_client.domain.tools import (
    ApplyPatchArguments,
    CommandHandleArguments,
    ListFilesArguments,
    LoadSkillArguments,
    McpCallArguments,
    McpPageArguments,
    McpPromptArguments,
    McpReadResourceArguments,
    McpSourceResult,
    McpToolEntries,
    ReadFileArguments,
    ReadOutputArguments,
    RunCommandArguments,
    SearchArguments,
    SearchTextArguments,
    SkillCatalogOmission,
    SkillEntries,
    SkillResourceArguments,
    ToolArguments,
    ToolContent,
    ToolDispatchEvent,
    ToolErrorContent,
    ToolName,
    ToolOutputEvent,
    ToolOutputProjection,
    ToolOutputRange,
    ToolPayload,
    WriteFileArguments,
)
from agent_client.domain.workspace import (
    FileListResult,
    FileReadResult,
    FileWriteResult,
    InstructionRules,
    InstructionSnapshot,
    PlatformKind,
    ProcessChannel,
    ProcessResult,
    RipgrepMatchRecord,
    RipgrepRecord,
    TextMatch,
    TextSearchResult,
)
from agent_client.infrastructure.mcp.manager import McpManager
from agent_client.infrastructure.workspace import files
from agent_client.infrastructure.workspace.process import (
    ProcessOutputSink,
    ProcessRunner,
    await_process_completion,
)

if TYPE_CHECKING:
    from agent_client.infrastructure.persistence.store import SessionStore


@dataclass
class OwnedCommand:
    handle: str
    session_id: str
    call_id: str
    task: asyncio.Task[ProcessResult]
    started: asyncio.Event


@dataclass
class WorkspaceLock:
    workspace: str
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass
class SessionInstructions:
    session_id: str
    snapshot: InstructionSnapshot


@dataclass
class WorkspaceInstructions:
    workspace: Path
    snapshot: InstructionSnapshot


@dataclass(frozen=True)
class ToolExecutionDefaults:
    startup_timeout: float = 5
    workspace_lock_timeout: float = 30
    process_preview_characters: int = 16000
    catalog_characters_per_token: int = 3


def specification(
    name: ToolName, description: str, parameters: type[Contract], effect: Effect = Effect.READ
) -> ToolSpec:
    return ToolSpec(
        name=name, description=description, effect=effect, parameters=parameters.model_json_schema()
    )


class ToolService:
    def __init__(self, config: AppConfig, store: "SessionStore"):
        self.config = config
        self.store = store
        self.output_projector = ToolOutputProjector(config.context, store)
        self.skills = SkillCatalog(config.skills.roots)
        self.resolver = InstructionResolver(store.home / "AGENTS.md")
        self.mcp = McpManager(config.mcp)
        self.process = ProcessRunner()
        self.rg = shutil.which("rg")
        self.locks: list[WorkspaceLock] = []
        self.defaults = ToolExecutionDefaults()
        self.commands: list[OwnedCommand] = []
        self.session_instructions: list[SessionInstructions] = []
        self.base_instructions: list[WorkspaceInstructions] = []

    def _workspace_lock(self, workspace: Path) -> asyncio.Lock:
        identity = str(workspace.resolve())
        owned = next((entry for entry in self.locks if entry.workspace == identity), None)
        if owned is None:
            owned = WorkspaceLock(identity)
            self.locks.append(owned)
        return owned.lock

    async def project_output(self, session_id: str, result: ToolResult) -> str:
        return await self.output_projector.project(session_id, result)

    async def start(self) -> None:
        if not self.rg:
            raise RuntimeError("ripgrep (rg) is required")
        version = await self.process.run(
            [self.rg, "--version"], Path.cwd(), timeout=self.defaults.startup_timeout
        )
        if version.exit_code != 0 or version.timed_out:
            raise RuntimeError("ripgrep executable failed its startup check")
        await self.skills.scan()
        await self.mcp.start()

    def specs(self) -> list[ToolSpec]:
        return [
            specification(
                ToolName.LIST_FILES,
                "List workspace files using ripgrep ignore rules",
                ListFilesArguments,
            ),
            specification(
                ToolName.READ_FILE, "Read numbered UTF-8 lines and content hash", ReadFileArguments
            ),
            specification(
                ToolName.SEARCH_TEXT,
                "Search exact text using ripgrep JSON output",
                SearchTextArguments,
            ),
            specification(
                ToolName.WRITE_FILE,
                "Atomic file replacement with expected sha256 or missing",
                WriteFileArguments,
                Effect.WRITE,
            ),
            specification(
                ToolName.APPLY_PATCH,
                "Replace one exact old_text occurrence with conflict protection",
                ApplyPatchArguments,
                Effect.WRITE,
            ),
            specification(
                ToolName.RUN_COMMAND,
                "Run a shell command; this is not an operating system sandbox",
                RunCommandArguments,
                Effect.PROCESS,
            ),
            specification(
                ToolName.POLL_COMMAND,
                "Poll an owned command handle; handles expire after restart",
                CommandHandleArguments,
            ),
            specification(
                ToolName.STOP_COMMAND,
                "Cancel an owned command and terminate its process tree",
                CommandHandleArguments,
                Effect.PROCESS,
            ),
            specification(
                ToolName.SEARCH_SKILLS, "Search configured skill metadata", SearchArguments
            ),
            specification(
                ToolName.LOAD_SKILL, "Load a skill body and fixed content hash", LoadSkillArguments
            ),
            specification(
                ToolName.READ_SKILL_RESOURCE,
                "Read a resource scoped to the skill directory",
                SkillResourceArguments,
            ),
            specification(
                ToolName.SEARCH_MCP_TOOLS,
                "Discover MCP tools, schemas and connection status. Use a short keyword or an empty query to list all tools; avoid long task descriptions.",
                SearchArguments,
            ),
            specification(
                ToolName.CALL_MCP_TOOL,
                "Call a discovered MCP tool after approval",
                McpCallArguments,
                Effect.REMOTE,
            ),
            specification(
                ToolName.READ_TOOL_OUTPUT,
                "Read a bounded range of saved full output",
                ReadOutputArguments,
            ),
            specification(
                ToolName.LIST_MCP_RESOURCES,
                "List one page of remote resource metadata",
                McpPageArguments,
            ),
            specification(
                ToolName.READ_MCP_RESOURCE,
                "Read a selected MCP resource as untrusted content",
                McpReadResourceArguments,
            ),
            specification(
                ToolName.LIST_MCP_PROMPTS, "List one page of MCP prompt metadata", McpPageArguments
            ),
            specification(
                ToolName.GET_MCP_PROMPT,
                "Fetch a selected prompt as source material",
                McpPromptArguments,
            ),
        ]

    async def instructions(self, workspace: Path) -> str:
        snapshot = await self.resolver.resolve(workspace)
        self.base_instructions = [
            entry for entry in self.base_instructions if entry.workspace != workspace.resolve()
        ]
        self.base_instructions.append(WorkspaceInstructions(workspace.resolve(), snapshot))
        catalog = SkillEntries(self.skills.search("")).model_dump_json()
        if (
            len(catalog)
            > self.config.skills.catalog_token_budget * self.defaults.catalog_characters_per_token
        ):
            catalog = SkillCatalogOmission(count=len(self.skills.entries)).model_dump_json()
        connections = McpServerDirectory(servers=self.mcp.status).model_dump_json()
        return (
            snapshot.render()
            + "\n\nSkill catalog:\n"
            + catalog
            + "\n\nMCP server connections (discover tool schemas with search_mcp_tools):\n"
            + connections
        )

    def _target(self, arguments: ToolArguments) -> str | None:
        match arguments:
            case (
                ListFilesArguments()
                | ReadFileArguments()
                | SearchTextArguments()
                | WriteFileArguments()
                | ApplyPatchArguments()
            ):
                return arguments.path
            case RunCommandArguments():
                return arguments.cwd
            case _:
                return None

    async def _rules(self, arguments: ToolArguments, context: ToolContext) -> InstructionRules:
        target = self._target(arguments)
        if target is None:
            return InstructionRules(content="", changed=False)
        previous = next(
            (
                entry.snapshot
                for entry in self.session_instructions
                if entry.session_id == context.session_id
            ),
            None,
        )
        if previous is None:
            previous = next(
                (
                    entry.snapshot
                    for entry in self.base_instructions
                    if entry.workspace == context.workspace.resolve()
                ),
                InstructionSnapshot(),
            )
        snapshot = await self.resolver.resolve(
            context.workspace, scoped_path(context.workspace, target)
        )
        changed = any(
            not any(
                old.path == document.path and old.sha256 == document.sha256
                for old in previous.documents
            )
            for document in snapshot.documents
        )
        paths = {document.path for document in snapshot.documents}
        merged = InstructionSnapshot(
            documents=[document for document in previous.documents if document.path not in paths]
            + snapshot.documents
        )
        self.session_instructions = [
            entry for entry in self.session_instructions if entry.session_id != context.session_id
        ]
        self.session_instructions.append(SessionInstructions(context.session_id, merged))
        return InstructionRules(content=snapshot.render(), changed=changed)

    def _result(
        self,
        call: ToolCall,
        content: ToolContent,
        status: ToolStatus = ToolStatus.SUCCEEDED,
        artifact_id: str | None = None,
        instructions: str | None = None,
    ) -> ToolResult:
        match content:
            case FileReadResult() | FileWriteResult() | ProcessResult() | ToolOutputProjection():
                content.applicable_instructions = instructions
        return ToolResult(
            call_id=call.id,
            content=content,
            status=status,
            is_error=status in {ToolStatus.FAILED, ToolStatus.DENIED, ToolStatus.UNKNOWN},
            artifact_id=artifact_id,
        )

    def _status(self, name: ToolName, content: ToolContent) -> ToolStatus:
        match content:
            case ProcessResult():
                if (
                    content.side_effect_result_unknown
                    or content.timed_out
                    and name in {ToolName.RUN_COMMAND, ToolName.POLL_COMMAND}
                ):
                    return ToolStatus.UNKNOWN
                if content.cancelled:
                    return ToolStatus.CANCELLED
                accepted_codes = (
                    {0, 1} if name in {ToolName.SEARCH_TEXT, ToolName.LIST_FILES} else {0}
                )
                if (
                    content.timed_out
                    or not content.running
                    and not content.cancelled
                    and content.exit_code not in accepted_codes
                ):
                    return ToolStatus.FAILED
                return ToolStatus.SUCCEEDED
            case McpToolResult() if content.is_error:
                return ToolStatus.FAILED
            case _:
                return ToolStatus.SUCCEEDED

    async def execute(
        self,
        call: ToolCall,
        context: ToolContext,
        emit: EventSink,
        approve: ApprovalHandler | None = None,
    ) -> ToolResult:
        dispatched = False
        effect = Effect.READ
        read_only = True
        try:
            name = ToolName(call.name)
            spec = next(spec for spec in self.specs() if spec.name == name)
            effect = spec.effect
            arguments = call.arguments
            read_only = effect == Effect.READ
            if context.approval_mode == ApprovalMode.READ_ONLY and effect != Effect.READ:
                return self._result(
                    call,
                    ToolErrorContent(error="Read-only mode denies this tool"),
                    ToolStatus.DENIED,
                )
            if isinstance(arguments, McpCallArguments):
                read_only = self.mcp.tool(arguments.tool_id).read_only_hint
            if context.unresolved_call_ids and not read_only:
                return self._result(
                    call,
                    ToolErrorContent(
                        error="Unverified side effects remain; reconcile their outcomes before another modifying action"
                    ),
                    ToolStatus.DENIED,
                )
            rules = await self._rules(arguments, context)
            if effect == Effect.WRITE and rules.changed:
                return self._result(
                    call,
                    ToolErrorContent(
                        error="Applicable instructions must be read before writing; review these rules then retry",
                        applicable_instructions=rules.content,
                    ),
                    ToolStatus.FAILED,
                )
            permission = (
                effect == Effect.WRITE
                and not context.allow_write
                or effect == Effect.PROCESS
                and name != ToolName.STOP_COMMAND
                and not context.allow_commands
                or effect == Effect.REMOTE
            )
            if context.approval_mode == ApprovalMode.ASK and permission:
                request = ApprovalRequest(
                    id=uuid.uuid4().hex,
                    call_id=call.id,
                    tool=name,
                    description=spec.description,
                    arguments=call.arguments,
                )
                if not approve or not await approve(request):
                    return self._result(
                        call, ToolErrorContent(error="Permission denied"), ToolStatus.DENIED
                    )
            event = ToolDispatchEvent(
                call_id=call.id, name=name, effect=effect, side_effecting=not read_only
            )
            await emit(
                RuntimeEvent(
                    kind=RuntimeEventKind.TOOL_DISPATCHING,
                    session_id=context.session_id,
                    run_id=context.run_id,
                    data=event,
                )
            )
            dispatched = True
            if effect == Effect.WRITE:
                lock = self._workspace_lock(context.workspace)
                locks_dir = context.home / "workspace-locks"
                await asyncio.to_thread(locks_dir.mkdir, parents=True, exist_ok=True)
                lock_name = (
                    hashlib.sha256(str(context.workspace.resolve()).encode()).hexdigest() + ".lock"
                )
                async with (
                    lock,
                    AsyncFileLock(
                        locks_dir / lock_name, timeout=self.defaults.workspace_lock_timeout
                    ),
                ):
                    payload = await self._execute(name, arguments, call, context, emit)
            else:
                payload = await self._execute(name, arguments, call, context, emit)
            status = self._status(name, payload.content)
            return self._result(call, payload.content, status, instructions=rules.content)
        except TimeoutError:
            return self._result(
                call,
                ToolErrorContent(
                    error="Read-only tool timed out"
                    if read_only
                    else "Tool timed out; side effect result may be unknown"
                ),
                ToolStatus.UNKNOWN if not read_only else ToolStatus.FAILED,
            )
        except (OSError, ValueError, KeyError, RuntimeError, SchemaValidationError) as error:
            unknown = dispatched and not read_only and isinstance(error, (RuntimeError, OSError))
            return self._result(
                call,
                ToolErrorContent(error=str(error)),
                ToolStatus.UNKNOWN if unknown else ToolStatus.FAILED,
            )

    async def _file_write(
        self, arguments: WriteFileArguments | ApplyPatchArguments, context: ToolContext
    ) -> FileWriteResult:
        path = scoped_path(context.workspace, arguments.path)
        snapshot = None
        if await asyncio.to_thread(path.exists):
            before = await asyncio.to_thread(path.read_text, encoding="utf-8")
            snapshot = await self.store.put_artifact(context.session_id, before)
        match arguments:
            case WriteFileArguments():
                task = asyncio.create_task(
                    asyncio.to_thread(
                        files.write_file,
                        context.workspace,
                        arguments.path,
                        arguments.content,
                        arguments.before_hash,
                    )
                )
            case ApplyPatchArguments():
                task = asyncio.create_task(
                    asyncio.to_thread(
                        files.apply_patch,
                        context.workspace,
                        arguments.path,
                        arguments.before_hash,
                        arguments.old_text,
                        arguments.new_text,
                    )
                )
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            result = await task
        result.before_artifact_id = snapshot
        after = await asyncio.to_thread(path.read_text, encoding="utf-8")
        result.after_artifact_id = await self.store.put_artifact(context.session_id, after)
        return result

    async def _search(
        self, arguments: ListFilesArguments | SearchTextArguments, context: ToolContext
    ) -> FileListResult | TextSearchResult:
        if self.rg is None:
            raise RuntimeError("ripgrep (rg) is required")
        path = scoped_path(context.workspace, arguments.path)
        match arguments:
            case ListFilesArguments():
                command = [self.rg, "--files", str(path)]
            case SearchTextArguments():
                command = [self.rg, "--json", "--fixed-strings", "--", arguments.query, str(path)]
        result = await self.process.run(command, context.workspace)
        if result.cancelled:
            raise asyncio.CancelledError
        match arguments:
            case ListFilesArguments():
                values = result.stdout.splitlines()
                return FileListResult(
                    files=values[: arguments.limit],
                    exit_code=result.exit_code,
                    stderr=result.stderr,
                    truncated=result.truncated or len(values) > arguments.limit,
                    timed_out=result.timed_out,
                )
            case SearchTextArguments():
                lines = result.stdout.splitlines()
                if result.truncated and not result.stdout.endswith("\n"):
                    lines = lines[:-1]
                matches: list[TextMatch] = []
                adapter = TypeAdapter(RipgrepRecord)
                for line in lines:
                    record = adapter.validate_json(line)
                    match record:
                        case RipgrepMatchRecord():
                            matches.append(record.data)
                return TextSearchResult(
                    matches=matches[: arguments.limit],
                    exit_code=result.exit_code,
                    stderr=result.stderr,
                    truncated=result.truncated or len(matches) > arguments.limit,
                    timed_out=result.timed_out,
                )

    async def _execute(
        self,
        name: ToolName,
        arguments: ToolArguments,
        call: ToolCall,
        context: ToolContext,
        emit: EventSink,
    ) -> ToolPayload:
        match arguments:
            case ReadFileArguments():
                content = await asyncio.to_thread(
                    files.read_file,
                    context.workspace,
                    arguments.path,
                    arguments.start,
                    arguments.end,
                )
            case WriteFileArguments() | ApplyPatchArguments():
                content = await self._file_write(arguments, context)
            case ListFilesArguments() | SearchTextArguments():
                content = await self._search(arguments, context)
            case RunCommandArguments():
                content = await self._launch(arguments, call, context, emit)
            case CommandHandleArguments():
                content = await self._poll(name, arguments, context, call.id)
            case SearchArguments():
                match name:
                    case ToolName.SEARCH_SKILLS:
                        content = SkillEntries(self.skills.search(arguments.query))
                    case ToolName.SEARCH_MCP_TOOLS:
                        content = McpToolEntries(
                            tools=self.mcp.search(arguments.query), servers=self.mcp.status
                        )
            case SkillResourceArguments():
                content = await self.skills.resource(arguments.skill_id, arguments.relative_path)
                content.snapshot_artifact_id = await self.store.put_artifact(
                    context.session_id, content.content
                )
            case LoadSkillArguments():
                content = await self.skills.load(arguments.skill_id)
                content.snapshot_artifact_id = await self.store.put_artifact(
                    context.session_id, content.content
                )
            case McpCallArguments():
                content = await self.mcp.call(
                    arguments.tool_id,
                    McpRequestArguments(
                        arguments=arguments.arguments, schema_hash=arguments.schema_hash
                    ),
                )
            case McpPageArguments():
                operation = (
                    McpOperation.LIST_RESOURCES
                    if name == ToolName.LIST_MCP_RESOURCES
                    else McpOperation.LIST_PROMPTS
                )
                content = McpSourceResult(
                    result=await self.mcp.request(
                        arguments.server, operation, McpRequestArguments(cursor=arguments.cursor)
                    )
                )
            case McpReadResourceArguments():
                content = McpSourceResult(
                    result=await self.mcp.request(
                        arguments.server,
                        McpOperation.READ_RESOURCE,
                        McpRequestArguments(uri=arguments.uri),
                    )
                )
            case McpPromptArguments():
                content = McpSourceResult(
                    result=await self.mcp.request(
                        arguments.server,
                        McpOperation.GET_PROMPT,
                        McpRequestArguments(name=arguments.name, arguments=arguments.arguments),
                    )
                )
            case ReadOutputArguments():
                body = await self.store.read_artifact(context.session_id, arguments.artifact_id)
                content = ToolOutputRange(
                    content=body[arguments.start : arguments.start + arguments.length],
                    total_characters=len(body),
                )
        return ToolPayload(content=content)

    async def _launch(
        self, arguments: RunCommandArguments, call: ToolCall, context: ToolContext, emit: EventSink
    ) -> ProcessResult:
        cwd = scoped_path(context.workspace, arguments.cwd)
        match PlatformKind(os.name):
            case PlatformKind.WINDOWS:
                command = [
                    "powershell",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new(); "
                    + arguments.command,
                ]
            case PlatformKind.POSIX:
                command = ["/bin/sh", "-c", arguments.command]

        async def output(channel: ProcessChannel, text: str) -> None:
            event = ToolOutputEvent(call_id=call.id, channel=channel, text=text)
            await emit(
                RuntimeEvent(
                    kind=RuntimeEventKind.TOOL_OUTPUT_CHUNK,
                    session_id=context.session_id,
                    run_id=context.run_id,
                    data=event,
                )
            )

        handle = uuid.uuid4().hex
        started = asyncio.Event()

        async def run_owned() -> ProcessResult:
            started.set()
            return await self._run_command(context, handle, command, cwd, arguments.timeout, output)

        task = asyncio.create_task(run_owned())
        self.commands.append(
            OwnedCommand(
                handle=handle,
                session_id=context.session_id,
                call_id=call.id,
                task=task,
                started=started,
            )
        )
        try:
            result = await asyncio.wait_for(asyncio.shield(task), arguments.yield_seconds)
            return result
        except TimeoutError:
            return ProcessResult(
                process_handle=handle,
                running=True,
                notice="Poll this process until terminal; handles do not survive restart",
            )
        except asyncio.CancelledError:
            task.cancel()
            await await_process_completion(asyncio.gather(task, return_exceptions=True))
            raise

    async def _poll(
        self, name: ToolName, arguments: CommandHandleArguments, context: ToolContext, call_id: str
    ) -> ProcessResult:
        owned = next(
            (entry for entry in self.commands if entry.handle == arguments.process_handle), None
        )
        if owned is None:
            raise ValueError("Unknown process handle")
        if owned.session_id != context.session_id:
            raise ValueError("Process handle belongs to another session")
        owned.call_id = call_id
        if name == ToolName.STOP_COMMAND and not owned.task.done():
            owned.task.cancel()
            await await_process_completion(asyncio.gather(owned.task, return_exceptions=True))
        if not owned.task.done():
            return ProcessResult(process_handle=arguments.process_handle, running=True)
        if owned.task.cancelled():
            return ProcessResult(
                process_handle=arguments.process_handle,
                cancelled=True,
                side_effect_result_unknown=owned.started.is_set(),
                notice=None
                if owned.started.is_set()
                else "Command cancelled before process launch",
            )
        result = owned.task.result()
        result.process_handle = arguments.process_handle
        return result

    async def _run_command(
        self,
        context: ToolContext,
        handle: str,
        command: list[str],
        cwd: Path,
        timeout: float,
        output: ProcessOutputSink,
    ) -> ProcessResult:
        directory = context.home / "temporary" / "commands" / handle
        locks_dir = context.home / "workspace-locks"
        lock_name = hashlib.sha256(str(context.workspace.resolve()).encode()).hexdigest() + ".lock"
        lock = self._workspace_lock(context.workspace)
        result: ProcessResult | None = None
        launched = False
        initializing: asyncio.Task[None] | None = None
        try:
            for path in (directory, locks_dir):
                initializing = asyncio.create_task(
                    asyncio.to_thread(path.mkdir, parents=True, exist_ok=True)
                )
                await asyncio.shield(initializing)
            async with (
                lock,
                AsyncFileLock(locks_dir / lock_name, timeout=self.defaults.workspace_lock_timeout),
            ):
                launched = True
                result = await self.process.run(
                    command,
                    cwd,
                    timeout,
                    limit=self.defaults.process_preview_characters,
                    on_output=output,
                    capture_dir=directory,
                )
            for channel in ProcessChannel:
                path = directory / channel
                if await asyncio.to_thread(path.exists):
                    artifact = await self.store.put_artifact_file(context.session_id, path)
                    match channel:
                        case ProcessChannel.STDOUT:
                            result.stdout_artifact_id = artifact
                        case ProcessChannel.STDERR:
                            result.stderr_artifact_id = artifact
            return result
        except asyncio.CancelledError:
            if initializing is not None:
                await await_process_completion(initializing)
            if result is not None:
                return result
            if launched:
                raise
            return ProcessResult(cancelled=True, notice="Command cancelled before process launch")
        finally:
            if await asyncio.to_thread(directory.exists):
                await asyncio.to_thread(shutil.rmtree, directory)

    async def close(self) -> None:
        pending = [owned.task for owned in self.commands if not owned.task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await await_process_completion(asyncio.gather(*pending, return_exceptions=True))
        self.commands.clear()
        await self.mcp.close()

    async def cancel_session(self, session_id: str) -> list[ProcessResult]:
        results: list[ProcessResult] = []
        for owned in list(self.commands):
            handle = owned.handle
            if owned.session_id != session_id:
                continue
            if not owned.task.done():
                owned.task.cancel()
                await await_process_completion(asyncio.gather(owned.task, return_exceptions=True))
            if owned.task.cancelled():
                results.append(
                    ProcessResult(
                        process_handle=handle,
                        tool_call_id=owned.call_id,
                        cancelled=True,
                        side_effect_result_unknown=owned.started.is_set(),
                        notice=None
                        if owned.started.is_set()
                        else "Command cancelled before process launch",
                    )
                )
                continue
            try:
                result = owned.task.result()
                result.process_handle = handle
                result.tool_call_id = owned.call_id
                results.append(result)
            except (OSError, ValueError, RuntimeError) as error:
                results.append(
                    ProcessResult(
                        process_handle=handle,
                        tool_call_id=owned.call_id,
                        error=str(error),
                        side_effect_result_unknown=True,
                    )
                )
        return results

    def owned_commands(self, session_id: str) -> set[str]:
        return {owned.handle for owned in self.commands if owned.session_id == session_id}

    def release_result(self, result: ToolResult | ProcessResult) -> None:
        match result:
            case ToolResult():
                if (
                    isinstance(result.content, (ProcessResult, ToolOutputProjection))
                    and result.content.running
                ):
                    return
                call_id = result.call_id
                process_handle = None
            case ProcessResult():
                if result.running:
                    return
                call_id = result.tool_call_id
                process_handle = result.process_handle
        for owned in list(self.commands):
            handle = owned.handle
            if (owned.call_id == call_id or handle == process_handle) and owned.task.done():
                self.commands.remove(owned)
