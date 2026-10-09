import argparse
import asyncio
import sys
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import cast

from pydantic import TypeAdapter

from agent_client.bootstrap import ClientServices
from agent_client.config import agent_home, load_config
from agent_client.domain.auth import CatalogModel
from agent_client.domain.enums import (
    ApprovalMode,
    AuthMode,
    ReasoningEffort,
    RunStatus,
    RuntimeEventKind,
    ToolStatus,
)
from agent_client.domain.errors import AgentError
from agent_client.domain.events import RuntimeEvent
from agent_client.domain.models import ApprovalRequest, SessionInfo
from agent_client.domain.presentation import (
    ApplicationLabel,
    AuthCommand,
    CLICommand,
    CLIOptions,
    DiagnosticReport,
)
from agent_client.domain.runtime import ContinuationAction, ErrorOccurred, TextDelta
from agent_client.infrastructure.auth import AuthService
from agent_client.infrastructure.models.access import ProviderAccess
from agent_client.infrastructure.persistence.maintenance import ProjectionMaintenance
from agent_client.presentation.tui import AgentApp


class ExitStatus(IntEnum):
    INTERRUPTED = 130


@dataclass
class ParsedCLI(argparse.Namespace):
    workspace: Path = field(default_factory=Path.cwd)
    reasoning_effort: ReasoningEffort | None = None
    approval_mode: ApprovalMode | None = None
    session: str | None = None
    config: Path | None = None
    home: Path | None = None
    allow_write: bool | None = None
    allow_commands: bool | None = None
    command: CLICommand | None = None
    auth_command: AuthCommand | None = None
    new_account: bool = False
    prompt: str | None = None
    session_id: str | None = None
    destination: Path | None = None
    call_id: str | None = None
    resolution: ToolStatus | None = None
    note: str | None = None

    def options(self) -> CLIOptions:
        return CLIOptions(
            workspace=self.workspace,
            reasoning_effort=self.reasoning_effort,
            approval_mode=self.approval_mode,
            session=self.session,
            config=self.config,
            home=self.home,
            allow_write=self.allow_write,
            allow_commands=self.allow_commands,
            command=self.command,
            auth_command=self.auth_command,
            new_account=self.new_account,
            prompt=self.prompt,
            session_id=self.session_id,
            destination=self.destination,
            call_id=self.call_id,
            resolution=self.resolution,
            note=self.note,
        )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Asynchronous local coding agent; opens a terminal UI by default"
    )
    result.add_argument("--workspace", type=Path, default=Path.cwd())
    result.add_argument("--session")
    result.add_argument("--config", type=Path)
    result.add_argument("--home", type=Path)
    result.add_argument("--allow-write", action="store_true", default=None)
    result.add_argument("--allow-commands", action="store_true", default=None)
    result.add_argument("--approval-mode", choices=list(ApprovalMode), default=None)
    result.add_argument("--reasoning-effort", choices=list(ReasoningEffort), default=None)
    commands = result.add_subparsers(dest="command")
    run = commands.add_parser(CLICommand.RUN.value, help="Run a task without the UI")
    run.add_argument("prompt")
    auth = commands.add_parser(CLICommand.AUTH.value, help="Manage provider authentication")
    authentication = auth.add_subparsers(dest="auth_command", required=True)
    login = authentication.add_parser(AuthCommand.LOGIN.value)
    login.add_argument("--new-account", action="store_true")
    for name in (AuthCommand.STATUS, AuthCommand.LOGOUT, AuthCommand.MODELS):
        authentication.add_parser(name.value)
    commands.add_parser(CLICommand.SESSIONS.value, help="List sessions")
    resume = commands.add_parser(
        CLICommand.RESUME.value, help="Restore a session; omit prompt to open the UI"
    )
    resume.add_argument("session_id")
    resume.add_argument("prompt", nargs="?")
    continuation = commands.add_parser(
        CLICommand.CONTINUE.value, help="Explicitly continue queued or interrupted work"
    )
    continuation.add_argument("session_id")
    backup = commands.add_parser(
        CLICommand.BACKUP.value, help="Back up session facts without credentials"
    )
    backup.add_argument("destination", type=Path)
    commands.add_parser(CLICommand.DIAGNOSTICS.value, help="Show nonsensitive runtime state")
    commands.add_parser(
        CLICommand.REBUILD.value, help="Rebuild SQLite from validated session journals"
    )
    deletion = commands.add_parser(
        CLICommand.DELETE.value, help="Move one inactive session to recoverable trash"
    )
    deletion.add_argument("session_id")
    resolve = commands.add_parser(
        CLICommand.RESOLVE.value, help="Record a tool reconciliation without replay"
    )
    resolve.add_argument("session_id")
    resolve.add_argument("call_id")
    resolve.add_argument(
        "resolution", choices=(ToolStatus.FAILED.value, ToolStatus.SUCCEEDED.value)
    )
    resolve.add_argument("note", help="Inspection evidence or an explicit reconciliation decision")
    return result


async def approve_cli(request: ApprovalRequest) -> bool:
    if not sys.stdin.isatty():
        print(
            f"Tool {request.tool} requires approval; noninteractive input defaults to deny.",
            file=sys.stderr,
        )
        return False
    print(request.arguments.model_dump_json(indent=2), file=sys.stderr)
    answer = await asyncio.to_thread(input, f"Allow once: {request.tool}? [y/N] ")
    return answer.strip().casefold() == "y"


async def emit_cli(event: RuntimeEvent) -> None:
    match event.kind:
        case RuntimeEventKind.TEXT_DELTA:
            delta = cast(TextDelta, event.data)
            print(delta.text, end="", flush=True)
        case RuntimeEventKind.ERROR:
            error = cast(ErrorOccurred, event.data)
            print(f"\n{error.code}: {error.message}", file=sys.stderr)
        case (
            RuntimeEventKind.TOOL_DISPATCHING
            | RuntimeEventKind.TOOL_OUTPUT_CHUNK
            | RuntimeEventKind.COMPACTION_STARTED
            | RuntimeEventKind.COMPACTION_FINISHED
        ):
            print(event.model_dump_json(), file=sys.stderr)


async def execute(args: CLIOptions) -> int:
    home = (args.home or agent_home()).expanduser().resolve()
    if args.command in {CLICommand.REBUILD, CLICommand.DELETE}:
        maintenance = ProjectionMaintenance(home)
        match args.command:
            case CLICommand.REBUILD:
                result = await maintenance.rebuild()
            case CLICommand.DELETE:
                result = await maintenance.delete_session(args.session_id)
        print(result.model_dump_json())
        return 0
    config = await asyncio.to_thread(load_config, args.config, home=home)
    if args.reasoning_effort is not None:
        config.model.select_reasoning(args.reasoning_effort)
    if args.command == CLICommand.AUTH:
        auth = AuthService(home)
        access = ProviderAccess(config.model, auth)
        try:
            match args.auth_command:
                case AuthCommand.LOGIN:
                    print(
                        "Opening system browser: Continue with ChatGPT."
                        if config.model.auth_mode == AuthMode.CHATGPT
                        else "Verifying the configured API key with the provider model catalog.",
                        file=sys.stderr,
                    )
                    result = await access.login(new_account=args.new_account)
                case AuthCommand.STATUS:
                    result = await access.status()
                case AuthCommand.LOGOUT:
                    result = await access.logout()
                case AuthCommand.MODELS:
                    models = await access.models()
                    print(TypeAdapter(list[CatalogModel]).dump_json(models).decode())
                    return 0
            print(result.model_dump_json(exclude_none=True))
            return 0
        finally:
            await auth.close()
    if args.approval_mode is not None:
        config.runtime.approval_mode = args.approval_mode
    if args.allow_write is not None:
        config.runtime.allow_write = args.allow_write
    if args.allow_commands is not None:
        config.runtime.allow_commands = args.allow_commands
    services = ClientServices.build(config, home)
    try:
        await services.open(
            start_tools=args.command
            not in {
                CLICommand.SESSIONS,
                CLICommand.BACKUP,
                CLICommand.DIAGNOSTICS,
                CLICommand.RESOLVE,
            }
        )
        match args.command:
            case CLICommand.SESSIONS:
                sessions = await services.store.list_sessions()
                print(TypeAdapter(list[SessionInfo]).dump_json(sessions).decode())
            case CLICommand.BACKUP:
                print(await services.store.backup(args.destination))
            case CLICommand.DIAGNOSTICS:
                report = DiagnosticReport(
                    home=home,
                    model=config.model.model,
                    auth_mode=config.model.auth_mode,
                    account=await services.access.status(),
                    sessions=await services.store.list_sessions(),
                    configured_mcp=sorted(config.mcp.servers),
                )
                print(report.model_dump_json())
            case CLICommand.RESOLVE:
                await services.runtime.resolve_unknown(
                    args.session_id, args.call_id, args.resolution, args.note
                )
                print("Reconciliation recorded. No tool was replayed.")
            case CLICommand.CONTINUE:
                identity = args.session_id
                plan = await services.runtime.prepare_continuation(identity)
                match plan.action:
                    case ContinuationAction.NO_TASK:
                        print("No task to continue.")
                        return 0
                    case ContinuationAction.QUEUED | ContinuationAction.PREPARED:
                        for item in plan.inputs:
                            result = await services.run(
                                identity,
                                item.prompt,
                                emit_cli,
                                approve_cli,
                                command_id=item.command_id,
                            )
                            print()
                            print(
                                f"status={result.status} stop_reason={result.stop_reason}",
                                file=sys.stderr,
                            )
                            if result.status != RunStatus.COMPLETED:
                                return 1
                        return 0
            case CLICommand.RUN | CLICommand.RESUME if args.prompt is not None:
                identity = args.session_id if args.command == CLICommand.RESUME else args.session
                if identity is not None:
                    await services.store.recover(identity)
                else:
                    identity = await services.store.create_session(
                        args.workspace, args.prompt[:100]
                    )
                command_id = await services.runtime.enqueue(identity, args.prompt)
                print(f"session_id={identity} command_id={command_id}", file=sys.stderr)
                for item in await services.runtime.pending_inputs(identity):
                    result = await services.run(
                        identity, item.prompt, emit_cli, approve_cli, command_id=item.command_id
                    )
                    print()
                    print(
                        f"status={result.status} stop_reason={result.stop_reason}", file=sys.stderr
                    )
                    if result.status != RunStatus.COMPLETED:
                        return 1
                return 0
            case _:
                identity = args.session_id if args.command == CLICommand.RESUME else args.session
                await AgentApp(services, args.workspace, identity).run_async()
        return 0
    finally:
        await services.close()


def main(argv: list[str] | None = None) -> int:
    print(ApplicationLabel.STARTUP, file=sys.stderr)
    namespace = parser().parse_args(argv, namespace=ParsedCLI())
    try:
        args = namespace.options()
        return asyncio.run(execute(args))
    except AgentError as error:
        print(f"{error.code}: {error.message}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Run cancelled. Use resume to inspect the session.", file=sys.stderr)
        return ExitStatus.INTERRUPTED
    except (ValueError, OSError) as error:
        print(
            f"Startup failed ({type(error).__name__}); check configuration and the data directory.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
