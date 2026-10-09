from enum import StrEnum
from typing import Literal

from pydantic import ConfigDict, Field, model_validator

from agent_client.domain.base import Contract
from agent_client.domain.enums import MessageRole
from agent_client.domain.protocol import ProtocolObject


class McpOperation(StrEnum):
    CALL = "call"
    LIST_RESOURCES = "list_resources"
    READ_RESOURCE = "read_resource"
    LIST_PROMPTS = "list_prompts"
    GET_PROMPT = "get_prompt"


class McpNegotiationMode(StrEnum):
    AUTO = "auto"


class McpCacheMode(StrEnum):
    REFRESH = "refresh"


class McpConnectionStatus(StrEnum):
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"


class McpContentType(StrEnum):
    TEXT = "text"
    IMAGE = "image"
    AUDIO = "audio"
    RESOURCE = "resource"
    RESOURCE_LINK = "resource_link"


class McpResultState(StrEnum):
    COMPLETE = "complete"


class McpCacheScope(StrEnum):
    PUBLIC = "public"
    PRIVATE = "private"


class McpTheme(StrEnum):
    LIGHT = "light"
    DARK = "dark"


class McpAnnotations(Contract):
    audience: list[Literal[MessageRole.USER, MessageRole.ASSISTANT]] | None = None
    priority: float | None = Field(default=None, ge=0, le=1)
    last_modified: str | None = None


class McpIcon(Contract):
    src: str
    mime_type: str | None = None
    sizes: list[str] | None = None
    theme: McpTheme | None = None


class McpResultMetadata(Contract):
    meta: ProtocolObject | None = None
    result_type: Literal[McpResultState.COMPLETE] = McpResultState.COMPLETE


class McpCacheMetadata(McpResultMetadata):
    ttl_ms: int | None = Field(default=None, ge=0)
    cache_scope: McpCacheScope = McpCacheScope.PRIVATE


class McpToolEntry(Contract):
    id: str
    server: str
    name: str
    description: str = ""
    parameters: ProtocolObject
    schema_hash: str
    read_only_hint: bool = False


class McpStatus(Contract):
    status: McpConnectionStatus
    protocol_version: str | None = None
    error: str | None = None


class McpNamedStatus(McpStatus):
    name: str


class McpServerDirectory(Contract):
    servers: list[McpNamedStatus] = Field(default_factory=list)


class McpResourceContent(Contract):
    model_config = ConfigDict(extra="forbid")
    uri: str
    text: str | None = None
    blob: str | None = None
    mime_type: str | None = None
    meta: ProtocolObject | None = None


class McpContent(Contract):
    model_config = ConfigDict(extra="forbid")
    type: McpContentType
    text: str | None = None
    data: str | None = None
    mime_type: str | None = None
    resource: McpResourceContent | None = None
    uri: str | None = None
    name: str | None = None
    title: str | None = None
    description: str | None = None
    size: int | None = Field(default=None, ge=0)
    icons: list[McpIcon] | None = None
    annotations: McpAnnotations | None = None
    meta: ProtocolObject | None = None

    @model_validator(mode="after")
    def validate_content(self):
        match self.type:
            case McpContentType.TEXT:
                if self.text is None:
                    raise ValueError("MCP text content is required")
            case McpContentType.IMAGE | McpContentType.AUDIO:
                if self.data is None or self.mime_type is None:
                    raise ValueError("MCP media data and MIME type are required")
            case McpContentType.RESOURCE:
                if self.resource is None:
                    raise ValueError("MCP embedded resource is required")
            case McpContentType.RESOURCE_LINK:
                if self.uri is None or self.name is None:
                    raise ValueError("MCP resource link identity is required")
        return self


class McpToolResult(McpResultMetadata):
    model_config = ConfigDict(extra="forbid")
    content: list[McpContent]
    structured_content: ProtocolObject | None = None
    is_error: bool = False


class McpResourceMetadata(Contract):
    model_config = ConfigDict(extra="forbid")
    uri: str
    name: str
    description: str | None = None
    mime_type: str | None = None
    title: str | None = None
    size: int | None = Field(default=None, ge=0)
    icons: list[McpIcon] | None = None
    annotations: McpAnnotations | None = None
    meta: ProtocolObject | None = None


class McpPromptArgument(Contract):
    model_config = ConfigDict(extra="forbid")
    name: str
    title: str | None = None
    description: str | None = None
    required: bool | None = None


class McpPromptMetadata(Contract):
    model_config = ConfigDict(extra="forbid")
    name: str
    description: str | None = None
    arguments: list[McpPromptArgument] | None = None
    title: str | None = None
    icons: list[McpIcon] | None = None
    meta: ProtocolObject | None = None


class McpPromptMessage(Contract):
    role: Literal[MessageRole.USER, MessageRole.ASSISTANT]
    content: McpContent


class McpResourcePage(McpCacheMetadata):
    model_config = ConfigDict(extra="forbid")
    resources: list[McpResourceMetadata]
    next_cursor: str | None = None


class McpResourceResult(McpCacheMetadata):
    model_config = ConfigDict(extra="forbid")
    contents: list[McpResourceContent]


class McpPromptPage(McpCacheMetadata):
    model_config = ConfigDict(extra="forbid")
    prompts: list[McpPromptMetadata]
    next_cursor: str | None = None


class McpPromptResult(McpResultMetadata):
    model_config = ConfigDict(extra="forbid")
    description: str | None = None
    messages: list[McpPromptMessage]


type McpOperationResult = (
    McpToolResult | McpResourcePage | McpResourceResult | McpPromptPage | McpPromptResult
)


class McpRequestArguments(Contract):
    name: str | None = None
    arguments: ProtocolObject = Field(default_factory=ProtocolObject)
    schema_hash: str | None = None
    cursor: str | None = None
    uri: str | None = None
