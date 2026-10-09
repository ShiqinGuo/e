import httpx
from pydantic import SecretStr

from agent_client.domain.configuration import ModelConfig
from agent_client.domain.models import ModelRequest
from agent_client.domain.protocol import (
    ContentType,
    NativeContent,
    NativeFunctionCall,
    NativeFunctionOutput,
    NativeReasoning,
    ProviderResponseStatus,
)
from agent_client.domain.provider import ResponsesRequest
from agent_client.domain.responses_input import ResponsesReasoningInput
from agent_client.infrastructure.models import ResponsesGateway


class Auth:
    async def access_token(self):
        return SecretStr("test-token")


async def test_reasoning_replay_preserves_evidence_without_adding_output_status():
    reasoning = NativeReasoning(
        id="rs_test",
        status=ProviderResponseStatus.COMPLETED,
        summary=[NativeContent(type=ContentType.SUMMARY_TEXT, text="A visible summary")],
        encrypted_content="opaque-provider-state",
    )
    original = reasoning.model_dump_json()
    async with httpx.AsyncClient() as client:
        gateway = ResponsesGateway(ModelConfig(), Auth(), client)
        request = ModelRequest(
            model="gpt-6.1-sol",
            instructions="Continue",
            items=[reasoning],
            cache_key="test",
        )
        body = gateway._body(request)
        wire = body.model_dump_json(exclude_none=True)
        assert '"status"' not in wire
        restored = ResponsesRequest.model_validate_json(wire)
        item = restored.input[0]
        assert isinstance(item, ResponsesReasoningInput)
        assert item.id == reasoning.id
        assert item.summary == reasoning.summary
        assert item.encrypted_content == reasoning.encrypted_content
        assert item.content is None
        assert reasoning.model_dump_json() == original


async def test_reasoning_encoding_keeps_native_tool_batch_order_and_visible_content():
    reasoning = NativeReasoning(
        id="rs_test",
        content=[NativeContent(type=ContentType.REASONING_TEXT, text="Visible provider content")],
    )
    call = NativeFunctionCall(call_id="call_test", name="read_file", arguments='{"path":"a"}')
    result = NativeFunctionOutput(call_id=call.call_id, output="A file")
    async with httpx.AsyncClient() as client:
        gateway = ResponsesGateway(ModelConfig(), Auth(), client)
        request = ModelRequest(
            model="gpt-6.1-sol",
            instructions="Continue",
            items=[reasoning, call, result],
            cache_key="test",
        )
        body = gateway._body(request)
        restored = ResponsesRequest.model_validate_json(body.model_dump_json(exclude_none=True))
        item = restored.input[0]
        assert isinstance(item, ResponsesReasoningInput)
        assert item.content == reasoning.content
        assert restored.input[1:] == [call, result]
