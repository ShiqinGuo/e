from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from agent_client.domain.base import Contract
from agent_client.domain.enums import (
    ApprovalMode,
    AuthMode,
    ChatReasoningMode,
    ContextStrategy,
    McpTransport,
    ProviderKind,
    ReasoningEffort,
)

type EnvironmentName = Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]


class EndpointScheme(StrEnum):
    HTTPS = "https"
    HTTP = "http"


class ModelConfig(Contract):
    provider: ProviderKind = ProviderKind.OPENAI_RESPONSES
    model: str = Field(default="gpt-6.1-sol", min_length=1)
    auth_mode: AuthMode = AuthMode.CHATGPT
    api_key_env: str = Field(default="OPENAI_API_KEY", pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    base_url: str = "https://api.openai.com/v1"
    reasoning_effort: ReasoningEffort = ReasoningEffort.MEDIUM
    context_window: int = Field(default=256000, ge=8192)
    max_output_tokens: int = Field(default=8192, ge=256)
    chat_reasoning: ChatReasoningMode = ChatReasoningMode.DEFAULT
    chat_stream_usage: bool = False
    chat_send_reasoning_effort: bool = False
    reasoning_levels: list[ReasoningEffort] | None = None

    def available_reasoning_levels(self) -> list[ReasoningEffort]:
        if self.reasoning_levels is not None:
            return self.reasoning_levels
        levels = [ReasoningEffort.LOW, ReasoningEffort.MEDIUM, ReasoningEffort.HIGH]
        if self.provider == ProviderKind.OPENAI_RESPONSES:
            levels.append(ReasoningEffort.XHIGH)
        if self.reasoning_effort not in levels:
            levels.append(self.reasoning_effort)
        return levels

    def select_reasoning(self, effort: ReasoningEffort) -> None:
        if effort not in self.available_reasoning_levels():
            raise ValueError("Reasoning effort is not enabled for this model profile")
        if (
            self.provider == ProviderKind.OPENAI_CHAT_COMPLETIONS
            and effort == ReasoningEffort.NONE
            and self.chat_reasoning == ChatReasoningMode.DEFAULT
        ):
            raise ValueError("This chat profile has no explicit thinking switch")
        self.reasoning_effort = effort
        if self.provider == ProviderKind.OPENAI_CHAT_COMPLETIONS:
            self.chat_send_reasoning_effort = True
            if self.chat_reasoning != ChatReasoningMode.DEFAULT and effort != ReasoningEffort.NONE:
                self.chat_reasoning = ChatReasoningMode.ENABLED

    @field_validator("reasoning_levels")
    @classmethod
    def validate_levels(cls, value: list[ReasoningEffort] | None):
        if value is not None and (not value or len(set(value)) != len(value)):
            raise ValueError("Reasoning levels must be nonempty and unique")
        return value

    @field_validator("base_url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError("Model endpoint port is invalid")
        if any(character.isspace() for character in value):
            raise ValueError("Model endpoint cannot contain whitespace")
        if parsed.scheme not in {EndpointScheme.HTTPS, EndpointScheme.HTTP} or not parsed.hostname:
            raise ValueError("Model endpoint must be an absolute HTTP URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Model endpoint cannot contain credentials, a query, or a fragment")
        return value

    @model_validator(mode="after")
    def validate_model(self):
        if (
            self.provider == ProviderKind.OPENAI_CHAT_COMPLETIONS
            and self.auth_mode != AuthMode.API_KEY
        ):
            raise ValueError("Chat Completions requires API key authentication")
        if (
            self.provider == ProviderKind.OPENAI_CHAT_COMPLETIONS
            and self.reasoning_effort == ReasoningEffort.NONE
            and self.chat_reasoning == ChatReasoningMode.DEFAULT
        ):
            raise ValueError("Disabling reasoning requires an explicit chat thinking switch")
        if self.provider != ProviderKind.OPENAI_CHAT_COMPLETIONS and (
            self.chat_reasoning != ChatReasoningMode.DEFAULT
            or self.chat_stream_usage
            or self.chat_send_reasoning_effort
        ):
            raise ValueError("Chat-only options require the Chat Completions provider")
        if self.reasoning_levels is not None and self.reasoning_effort not in self.reasoning_levels:
            raise ValueError("Selected reasoning effort is not in the configured model levels")
        if self.max_output_tokens >= self.context_window:
            raise ValueError("Output reserve leaves no input context")
        if (
            self.auth_mode == AuthMode.CHATGPT
            and self.base_url.rstrip("/") != "https://api.openai.com/v1"
        ):
            raise ValueError("ChatGPT credentials can only be sent to the official API")
        return self


class RuntimeConfig(Contract):
    approval_mode: ApprovalMode = ApprovalMode.ASK
    max_model_steps: int = Field(default=40, ge=1, le=1000)
    max_tool_calls: int = Field(default=120, ge=1, le=10000)
    max_parallel_reads: int = Field(default=4, ge=1, le=32)
    deadline_seconds: float = Field(default=1800, gt=0)
    allow_write: bool = False
    allow_commands: bool = False


class ContextConfig(Contract):
    soft_ratio: float = Field(default=0.95, gt=0.1, lt=1)
    target_ratio: float = Field(default=0.25, gt=0.05, lt=1)
    strategy: ContextStrategy = ContextStrategy.SUMMARY
    token_bytes_per_token: float = Field(default=2, gt=0)
    estimate_overhead_tokens: int = Field(default=16, ge=0)
    reserve_min_tokens: int = Field(default=1024, ge=0)
    reserve_ratio: float = Field(default=0.01, ge=0, lt=1)
    summary_max_output_tokens: int = Field(default=8192, ge=256)
    summary_reasoning_effort: ReasoningEffort | None = None
    tool_output_characters: int = Field(default=16000, ge=1024)
    tool_output_tail_ratio: float = Field(default=0.25, gt=0, lt=1)

    @model_validator(mode="after")
    def validate_ratios(self):
        if self.target_ratio >= self.soft_ratio:
            raise ValueError("Compaction target must be smaller than its trigger")
        return self


class SkillsConfig(Contract):
    roots: list[Path] = Field(default_factory=list)
    catalog_token_budget: int = Field(default=2000, ge=100)


class EnvironmentBinding(Contract):
    name: EnvironmentName
    reference: EnvironmentName


class McpServerConfig(Contract):
    transport: McpTransport
    command: str | None = Field(default=None, min_length=1)
    args: list[str] = Field(default_factory=list)
    url: str | None = None
    required: bool = False
    env: list[EnvironmentBinding] = Field(default_factory=list)

    @field_validator("env", mode="before")
    @classmethod
    def parse_environment(cls, value: object):
        if isinstance(value, Mapping):
            return [
                EnvironmentBinding(name=name, reference=reference)
                for name, reference in value.items()
            ]
        return value

    token_env: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    timeout_seconds: float = Field(default=60, gt=0)

    @model_validator(mode="after")
    def validate_transport(self):
        match self.transport:
            case McpTransport.STDIO if not self.command:
                raise ValueError("Stdio MCP requires a command")
            case McpTransport.STREAMABLE_HTTP if not self.url:
                raise ValueError("HTTP MCP requires a URL")
        if self.url:
            parsed = urlsplit(self.url)
            if parsed.port is not None and not 1 <= parsed.port <= 65535:
                raise ValueError("MCP endpoint port is invalid")
            if any(character.isspace() for character in self.url):
                raise ValueError("MCP endpoint cannot contain whitespace")
            if (
                parsed.scheme not in {EndpointScheme.HTTPS, EndpointScheme.HTTP}
                or not parsed.hostname
            ):
                raise ValueError("MCP endpoint must be an absolute HTTP URL")
            if parsed.username or parsed.password or parsed.fragment:
                raise ValueError("MCP endpoint cannot contain credentials or a fragment")
        return self


class NamedMcpServer(McpServerConfig):
    name: str = Field(min_length=1)


class McpConfig(Contract):
    servers: list[NamedMcpServer] = Field(default_factory=list)

    @field_validator("servers", mode="before")
    @classmethod
    def parse_servers(cls, value: object):
        if isinstance(value, Mapping):
            servers: list[NamedMcpServer] = []
            for name, configuration in value.items():
                if not isinstance(configuration, Mapping):
                    raise ValueError("MCP server configuration must be a table")
                parsed = McpServerConfig.model_validate(configuration)
                servers.append(
                    NamedMcpServer(
                        name=name,
                        transport=parsed.transport,
                        command=parsed.command,
                        args=parsed.args,
                        url=parsed.url,
                        required=parsed.required,
                        env=parsed.env,
                        token_env=parsed.token_env,
                        timeout_seconds=parsed.timeout_seconds,
                    )
                )
            return servers
        return value

    @model_validator(mode="after")
    def validate_names(self):
        names = [server.name for server in self.servers]
        if len(set(names)) != len(names):
            raise ValueError("MCP server names must be unique")
        return self


class AppConfig(Contract):
    model: ModelConfig = Field(default_factory=ModelConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    skills: SkillsConfig = Field(default_factory=SkillsConfig)
    mcp: McpConfig = Field(default_factory=McpConfig)

    def summary_effort(self) -> ReasoningEffort:
        if self.context.summary_reasoning_effort is not None:
            return self.context.summary_reasoning_effort
        match self.model.provider:
            case ProviderKind.OPENAI_RESPONSES:
                return ReasoningEffort.LOW
            case ProviderKind.OPENAI_CHAT_COMPLETIONS:
                if self.model.chat_reasoning != ChatReasoningMode.DEFAULT:
                    return ReasoningEffort.NONE
                return self.model.reasoning_effort

    @model_validator(mode="after")
    def validate_context_reserves(self):
        reserve = max(
            self.context.reserve_min_tokens,
            int(self.model.context_window * self.context.reserve_ratio),
        )
        if (
            max(self.model.max_output_tokens, self.context.summary_max_output_tokens) + reserve
            >= self.model.context_window
        ):
            raise ValueError("Output and safety reserves leave no input context")
        return self
