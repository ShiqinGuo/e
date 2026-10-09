from typing import TYPE_CHECKING

from pydantic import ValidationError

from agent_client.domain.configuration import ContextConfig
from agent_client.domain.mcp import (
    McpContent,
    McpContentType,
    McpPromptResult,
    McpResourceResult,
    McpToolResult,
)
from agent_client.domain.models import ToolResult
from agent_client.domain.protocol import ProtocolObject
from agent_client.domain.runtime import TruncatedToolOutput
from agent_client.domain.tools import McpContextContent, McpSourceResult, ToolContent

if TYPE_CHECKING:
    from agent_client.infrastructure.persistence.store import SessionStore


class ToolOutputProjector:
    def __init__(self, config: ContextConfig, store: "SessionStore"):
        self.config = config
        self.store = store

    def _text_parts(self, parts: list[McpContent]) -> list[str]:
        text: list[str] = []
        for part in parts:
            match part.type:
                case McpContentType.TEXT:
                    text.append(part.text)
                case McpContentType.RESOURCE:
                    text.append(part.resource.uri)
                    if part.resource.text is not None:
                        text.append(part.resource.text)
                case McpContentType.RESOURCE_LINK:
                    text.append(f"{part.name}: {part.uri}")
                case McpContentType.IMAGE | McpContentType.AUDIO:
                    text.append(
                        f"{part.type} ({part.mime_type}); binary data retained in the full tool result"
                    )
        return text

    def _model_content(self, content: ToolContent, artifact_id: str | None) -> str:
        match content:
            case McpToolResult():
                text = self._text_parts(content.content)
                structured = content.structured_content
                for part in content.content:
                    if part.type != McpContentType.TEXT or structured is None:
                        continue
                    try:
                        parsed = ProtocolObject.model_validate(part.text)
                    except ValidationError:
                        continue
                    if parsed == structured:
                        structured = None
                is_error = content.is_error
            case McpSourceResult(result=McpResourceResult() as resources):
                text = [
                    f"{resource.uri}\n{resource.text}"
                    if resource.text is not None
                    else f"{resource.uri}: binary resource retained in the full tool result"
                    for resource in resources.contents
                ]
                structured = None
                is_error = False
            case McpSourceResult(result=McpPromptResult() as prompt):
                text = self._text_parts([message.content for message in prompt.messages])
                if prompt.description is not None:
                    text.insert(0, prompt.description)
                structured = None
                is_error = False
            case _:
                return content.model_dump_json(exclude_none=True)
        return McpContextContent(
            text=text,
            structured_content=structured,
            artifact_id=artifact_id,
            is_error=is_error,
        ).model_dump_json(exclude_none=True)

    async def project(self, session_id: str, result: ToolResult) -> str:
        full = result.content.model_dump_json(exclude_none=True)
        if (
            isinstance(result.content, (McpToolResult, McpSourceResult))
            and result.artifact_id is None
        ):
            result.artifact_id = await self.store.put_artifact(session_id, full)
        projected = self._model_content(result.content, result.artifact_id)
        limit = self.config.tool_output_characters
        if len(projected) <= limit:
            return projected
        if result.artifact_id is None:
            result.artifact_id = await self.store.put_artifact(session_id, full)

        def render(retained: int) -> str:
            tail = int(retained * self.config.tool_output_tail_ratio)
            head = retained - tail
            return TruncatedToolOutput(
                head=projected[:head],
                tail=projected[-tail:] if tail else "",
                length=len(projected),
                artifact_id=result.artifact_id,
                status=result.status,
            ).model_dump_json()

        lower = 0
        upper = min(limit, len(projected))
        while lower < upper:
            retained = (lower + upper + 1) // 2
            if len(render(retained)) <= limit:
                lower = retained
            else:
                upper = retained - 1
        return render(lower)
