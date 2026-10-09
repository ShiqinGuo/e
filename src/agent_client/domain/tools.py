from enum import StrEnum

from pydantic import Field, RootModel, SerializeAsAny, model_validator

from agent_client.domain.base import Contract, Effect
from agent_client.domain.mcp import McpOperationResult, McpServerDirectory, McpToolEntry
from agent_client.domain.protocol import ProtocolObject
from agent_client.domain.skills import SkillEntry, SkillLoadResult, SkillResourceResult
from agent_client.domain.workspace import (
    FileListResult,
    FileReadResult,
    FileVersion,
    FileWriteResult,
    ProcessChannel,
    ProcessResult,
    TextSearchResult,
)


class ToolName(StrEnum):
    LIST_FILES = "list_files"
    READ_FILE = "read_file"
    SEARCH_TEXT = "search_text"
    WRITE_FILE = "write_file"
    APPLY_PATCH = "apply_patch"
    RUN_COMMAND = "run_command"
    POLL_COMMAND = "poll_command"
    STOP_COMMAND = "stop_command"
    SEARCH_SKILLS = "search_skills"
    LOAD_SKILL = "load_skill"
    READ_SKILL_RESOURCE = "read_skill_resource"
    SEARCH_MCP_TOOLS = "search_mcp_tools"
    CALL_MCP_TOOL = "call_mcp_tool"
    READ_TOOL_OUTPUT = "read_tool_output"
    LIST_MCP_RESOURCES = "list_mcp_resources"
    READ_MCP_RESOURCE = "read_mcp_resource"
    LIST_MCP_PROMPTS = "list_mcp_prompts"
    GET_MCP_PROMPT = "get_mcp_prompt"


class JsonSchemaType(StrEnum):
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    OBJECT = "object"
    ARRAY = "array"
    BOOLEAN = "boolean"
    NULL = "null"


class ContentSource(StrEnum):
    UNTRUSTED_MCP = "untrusted MCP content"


class ToolPathArguments(Contract):
    path: str = Field(default=".", min_length=1)


class ListFilesArguments(ToolPathArguments):
    limit: int = Field(default=200, ge=1, le=5000)


class ReadFileArguments(Contract):
    path: str = Field(min_length=1)
    start: int = Field(default=1, ge=1)
    end: int = Field(default=400, ge=1)

    @model_validator(mode="after")
    def validate_range(self):
        if self.start > self.end or self.end - self.start > 2000:
            raise ValueError("Invalid line range; maximum 2001 lines")
        return self


class SearchTextArguments(ToolPathArguments):
    query: str
    limit: int = Field(default=200, ge=1, le=1000)


class WriteFileArguments(Contract):
    path: str = Field(min_length=1)
    content: str
    before_hash: str = Field(pattern=rf"^(?:[a-f0-9]{{64}}|{FileVersion.MISSING})$")


class ApplyPatchArguments(Contract):
    path: str = Field(min_length=1)
    before_hash: str = Field(pattern=rf"^(?:[a-f0-9]{{64}}|{FileVersion.MISSING})$")
    old_text: str = Field(min_length=1)
    new_text: str


class RunCommandArguments(Contract):
    command: str = Field(min_length=1)
    cwd: str = Field(default=".", min_length=1)
    timeout: float = Field(default=60, gt=0, le=600)
    yield_seconds: float = Field(default=10, ge=0, le=30)


class CommandHandleArguments(Contract):
    process_handle: str = Field(min_length=1)


class SearchArguments(Contract):
    query: str


class LoadSkillArguments(Contract):
    skill_id: str = Field(min_length=1)


class SkillResourceArguments(LoadSkillArguments):
    relative_path: str = Field(min_length=1)


class McpCallArguments(Contract):
    tool_id: str = Field(min_length=1)
    schema_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    arguments: ProtocolObject


class ReadOutputArguments(Contract):
    artifact_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    start: int = Field(default=0, ge=0)
    length: int = Field(default=16000, ge=1, le=32000)


class McpPageArguments(Contract):
    server: str = Field(min_length=1)
    cursor: str | None = None


class McpReadResourceArguments(Contract):
    server: str = Field(min_length=1)
    uri: str = Field(min_length=1)


class McpPromptArguments(Contract):
    server: str = Field(min_length=1)
    name: str = Field(min_length=1)
    arguments: ProtocolObject = Field(default_factory=ProtocolObject)


type ToolArguments = (
    ListFilesArguments
    | ReadFileArguments
    | SearchTextArguments
    | WriteFileArguments
    | ApplyPatchArguments
    | RunCommandArguments
    | CommandHandleArguments
    | SearchArguments
    | LoadSkillArguments
    | SkillResourceArguments
    | McpCallArguments
    | ReadOutputArguments
    | McpPageArguments
    | McpReadResourceArguments
    | McpPromptArguments
)


class ToolErrorContent(Contract):
    error: str
    applicable_instructions: str | None = None


class ToolOutputRange(Contract):
    content: str
    total_characters: int


class ToolOutputProjection(Contract):
    preview: str
    tail: str
    truncated: bool = True
    total_characters: int
    artifact_id: str
    exit_code: int | None = None
    timed_out: bool = False
    running: bool = False
    process_handle: str | None = None
    stdout_artifact_id: str | None = None
    stderr_artifact_id: str | None = None
    full_output_truncated: bool = False
    applicable_instructions: str | None = None


class SkillEntries(RootModel[list[SkillEntry]]):
    pass


class McpToolEntries(McpServerDirectory):
    tools: list[McpToolEntry] = Field(default_factory=list)
    notice: str = "Search uses all query words. Use a short keyword or an empty query to list all tools. Disconnected servers have no discoverable tools; inspect their errors."


class SkillCatalogOmission(Contract):
    notice: str = "Catalog exceeds prompt budget; use search_skills"
    count: int


class McpSourceResult(Contract):
    source: ContentSource = ContentSource.UNTRUSTED_MCP
    result: McpOperationResult


class McpContextContent(Contract):
    artifact_id: str | None = None
    is_error: bool = False
    source: ContentSource = ContentSource.UNTRUSTED_MCP
    text: list[str] = Field(default_factory=list)
    structured_content: ProtocolObject | None = None


type ToolContent = (
    FileReadResult
    | FileWriteResult
    | ProcessResult
    | FileListResult
    | TextSearchResult
    | SkillLoadResult
    | SkillResourceResult
    | SkillEntries
    | McpToolEntries
    | McpOperationResult
    | McpSourceResult
    | ToolOutputRange
    | ToolErrorContent
    | ToolOutputProjection
)


class ToolPayload(Contract):
    content: SerializeAsAny[ToolContent]
    applicable_instructions: str | None = None


class ToolDispatchEvent(Contract):
    side_effecting: bool = True
    call_id: str
    name: ToolName
    effect: Effect


class ToolOutputEvent(Contract):
    call_id: str
    channel: ProcessChannel
    text: str


def argument_model(name: ToolName) -> type[ToolArguments]:
    match name:
        case ToolName.LIST_FILES:
            return ListFilesArguments
        case ToolName.READ_FILE:
            return ReadFileArguments
        case ToolName.SEARCH_TEXT:
            return SearchTextArguments
        case ToolName.WRITE_FILE:
            return WriteFileArguments
        case ToolName.APPLY_PATCH:
            return ApplyPatchArguments
        case ToolName.RUN_COMMAND:
            return RunCommandArguments
        case ToolName.POLL_COMMAND | ToolName.STOP_COMMAND:
            return CommandHandleArguments
        case ToolName.SEARCH_SKILLS | ToolName.SEARCH_MCP_TOOLS:
            return SearchArguments
        case ToolName.LOAD_SKILL:
            return LoadSkillArguments
        case ToolName.READ_SKILL_RESOURCE:
            return SkillResourceArguments
        case ToolName.CALL_MCP_TOOL:
            return McpCallArguments
        case ToolName.READ_TOOL_OUTPUT:
            return ReadOutputArguments
        case ToolName.LIST_MCP_RESOURCES | ToolName.LIST_MCP_PROMPTS:
            return McpPageArguments
        case ToolName.READ_MCP_RESOURCE:
            return McpReadResourceArguments
        case ToolName.GET_MCP_PROMPT:
            return McpPromptArguments
