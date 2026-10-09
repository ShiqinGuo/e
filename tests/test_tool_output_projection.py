import pytest

from agent_client.application.tool_output import ToolOutputProjector
from agent_client.domain.configuration import ContextConfig
from agent_client.domain.mcp import (
    McpContent,
    McpContentType,
    McpResourceContent,
    McpResourceResult,
    McpToolResult,
)
from agent_client.domain.models import ToolResult
from agent_client.domain.protocol import ProtocolObject
from agent_client.domain.runtime import TruncatedToolOutput
from agent_client.domain.tools import McpContextContent, McpSourceResult
from agent_client.domain.workspace import ProcessResult
from agent_client.infrastructure.persistence.store import SessionStore


@pytest.mark.asyncio
async def test_existing_artifact_does_not_bypass_context_budget(tmp_path):
    store = SessionStore(tmp_path)
    await store.open()
    try:
        session = await store.create_session(tmp_path)
        content = ProcessResult(stdout='"\\\n' * 30000, exit_code=0)
        artifact = await store.put_artifact(session, content.model_dump_json())
        result = ToolResult(call_id="call", content=content, artifact_id=artifact)
        output = await ToolOutputProjector(ContextConfig(), store).project(session, result)
        projected = TruncatedToolOutput.model_validate_json(output)
        assert len(output) <= ContextConfig().tool_output_characters
        assert projected.artifact_id == artifact
        assert result.content.stdout == content.stdout
        assert await store.read_artifact(session, artifact) == content.model_dump_json()
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_binary_resource_is_saved_without_base64_in_model_context(tmp_path):
    store = SessionStore(tmp_path)
    await store.open()
    try:
        session = await store.create_session(tmp_path)
        source = McpSourceResult(
            result=McpResourceResult(
                contents=[McpResourceContent(uri="file:///image.png", blob="c2VjcmV0LWJpbmFyeQ==")]
            )
        )
        result = ToolResult(call_id="resource", content=source)
        output = await ToolOutputProjector(ContextConfig(), store).project(session, result)
        assert "c2VjcmV0LWJpbmFyeQ==" not in output
        assert "file:///image.png" in output
        assert result.artifact_id in output
        assert "c2VjcmV0LWJpbmFyeQ==" in await store.read_artifact(session, result.artifact_id)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_mcp_duplicate_content_is_projected_once_and_full_result_retained(tmp_path):
    store = SessionStore(tmp_path)
    await store.open()
    try:
        session = await store.create_session(tmp_path)
        structured = ProtocolObject.model_validate('{"price":123,"unit":"USD"}')
        content = McpToolResult(
            content=[McpContent(type=McpContentType.TEXT, text=structured.root)],
            structured_content=structured,
        )
        result = ToolResult(call_id="call", content=content)
        output = await ToolOutputProjector(ContextConfig(), store).project(session, result)
        projected = McpContextContent.model_validate_json(output)
        assert projected.structured_content is None
        assert projected.text == [structured.root]
        assert projected.artifact_id == result.artifact_id
        restored = McpToolResult.model_validate_json(
            await store.read_artifact(session, result.artifact_id)
        )
        assert restored.structured_content == structured
    finally:
        await store.close()
