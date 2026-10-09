from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from agent_client.domain.auth import AuthStatus
from agent_client.domain.base import Contract
from agent_client.domain.enums import (
    ApprovalMode,
    AuthMode,
    ReasoningEffort,
    ToolStatus,
)
from agent_client.domain.mcp import McpNamedStatus
from agent_client.domain.models import SessionInfo
from agent_client.domain.protocol import TokenUsage
from agent_client.domain.runtime import ModelMetrics


class ApplicationLabel(StrEnum):
    STARTUP = "Agent Client · ShaneGuo"


class CLICommand(StrEnum):
    RUN = "run"
    AUTH = "auth"
    SESSIONS = "sessions"
    RESUME = "resume"
    CONTINUE = "continue"
    BACKUP = "backup"
    DIAGNOSTICS = "diagnostics"
    RESOLVE = "resolve"
    REBUILD = "rebuild"
    DELETE = "delete"


class AuthCommand(StrEnum):
    LOGIN = "login"
    STATUS = "status"
    LOGOUT = "logout"
    MODELS = "models"


class SlashCommand(StrEnum):
    REASONING = "/reasoning"
    PERMISSIONS = "/permissions"
    NEW = "/new"
    RESUME = "/resume"
    CONTINUE = "/continue"
    SESSIONS = "/sessions"
    LOGIN = "/login"
    LOGOUT = "/logout"
    MODEL = "/model"
    SKILLS = "/skills"
    MCP = "/mcp"
    COMPACT = "/compact"
    RESOLVE = "/resolve"
    STATUS = "/status"
    CONFIG = "/config"
    REBUILD = "/rebuild"
    DELETE = "/delete"


class WidgetID(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    APPROVAL_CHOICES = "approval-choices"
    PERMISSIONS = "permissions"
    REASONING_LEVELS = "reasoning-levels"
    ACCOUNT = "account"
    USAGE = "usage"
    TRANSCRIPT = "transcript"
    COMPOSER = "composer"
    APPROVAL = "approval"
    APPROVAL_DETAILS = "approval-details"
    APPROVAL_DESCRIPTION = "approval-description"
    COMMAND_MENU = "command-menu"
    RUN_STATUS = "run-status"
    CONTEXT_USAGE = "context-usage"

    @property
    def selector(self) -> str:
        return f"#{self.value}"


class ComposerKey(StrEnum):
    ENTER = "enter"
    SHIFT_ENTER = "shift+enter"
    CTRL_ENTER = "ctrl+enter"
    CTRL_J = "ctrl+j"
    UP = "up"
    DOWN = "down"
    TAB = "tab"


class SlashCommandHelp(Contract):
    command: SlashCommand
    description: str = Field(min_length=1)
    arguments: str = ""


class LoginIntent(StrEnum):
    NEW = "new"


class CLIOptions(Contract):
    reasoning_effort: ReasoningEffort | None = None
    approval_mode: ApprovalMode | None = None
    workspace: Path
    session: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_-]{1,100}$")
    config: Path | None = None
    home: Path | None = None
    allow_write: bool | None = None
    allow_commands: bool | None = None
    command: CLICommand | None = None
    auth_command: AuthCommand | None = None
    new_account: bool = False
    prompt: str | None = None
    session_id: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_-]{1,100}$")
    destination: Path | None = None
    call_id: str | None = None
    resolution: Literal[ToolStatus.FAILED, ToolStatus.SUCCEEDED] | None = None
    note: str | None = None

    @model_validator(mode="after")
    def validate_command(self):
        match self.command:
            case CLICommand.RUN:
                if not self.prompt:
                    raise ValueError("Run requires a prompt")
            case CLICommand.RESUME | CLICommand.CONTINUE:
                if self.session_id is None:
                    raise ValueError("Resume requires a session")
            case CLICommand.DELETE:
                if self.session_id is None:
                    raise ValueError("Deletion requires a session")
            case CLICommand.AUTH:
                if self.auth_command is None:
                    raise ValueError("Authentication command is required")
            case CLICommand.BACKUP:
                if self.destination is None:
                    raise ValueError("Backup requires a destination")
            case CLICommand.RESOLVE:
                if (
                    self.session_id is None
                    or self.call_id is None
                    or self.resolution is None
                    or not self.note
                ):
                    raise ValueError("Resolution requires a session, call, outcome and evidence")
        return self


class DiagnosticReport(Contract):
    home: Path
    model: str
    auth_mode: AuthMode
    account: AuthStatus
    sessions: list[SessionInfo]
    configured_mcp: list[str]
    credentials_included: bool = False
    usage: TokenUsage | None = None


class DisplayStatus(Contract):
    approval_mode: ApprovalMode = ApprovalMode.ASK
    session_id: str | None
    model: str
    workspace: Path
    pending: int = Field(ge=0)
    allow_write: bool
    allow_commands: bool
    logs: Path
    usage: ModelMetrics | None = None


class McpStatusView(Contract):
    servers: list[McpNamedStatus]
