import pytest
from pydantic import ValidationError

from agent_client.domain.mcp import McpContent, McpContentType
from agent_client.domain.protocol import NATIVE_ITEM_ADAPTER, NativeReasoning, ProtocolObject


@pytest.mark.parametrize("payload", ['{"n": NaN}', '{"n": 1, "n": 2}', "[]", "null"])
def test_protocol_object_rejects_ambiguous_or_non_object_payloads(payload):
    with pytest.raises(ValidationError):
        ProtocolObject(payload)


def test_protocol_object_default_serializes_as_object():
    assert ProtocolObject().model_dump_json() == "{}"


def test_native_reasoning_preserves_opaque_replay_payload():
    item = NATIVE_ITEM_ADAPTER.validate_json(
        '{"type":"reasoning","id":"reasoning-1","encrypted_content":"opaque"}'
    )
    assert isinstance(item, NativeReasoning)
    assert item.encrypted_content == "opaque"


def test_native_function_call_requires_identity_at_boundary():
    with pytest.raises(ValidationError):
        NATIVE_ITEM_ADAPTER.validate_json('{"type":"function_call","name":"read_file"}')


def test_mcp_text_requires_text_at_boundary():
    with pytest.raises(ValidationError):
        McpContent(type=McpContentType.TEXT)


def test_tool_result_rejects_unknown_business_content():
    from agent_client.domain.models import ToolResult

    with pytest.raises(ValidationError):
        ToolResult.model_validate_json('{"call_id":"call-1","content":{"garbage":"unrecognized"}}')


def test_native_message_preserves_phase_annotations_and_logprobs():
    from agent_client.domain.protocol import MessagePhase, NativeMessage

    wire = '{"type":"message","id":"message-1","role":"assistant","status":"completed","phase":"commentary","content":[{"type":"output_text","text":"Source","annotations":[{"type":"url_citation","start_index":0,"end_index":6,"title":"Reference","url":"https://example.test"}],"logprobs":[{"token":"Source","bytes":[83],"logprob":-0.2,"top_logprobs":[]}]}]}'
    item = NATIVE_ITEM_ADAPTER.validate_json(wire)
    assert isinstance(item, NativeMessage)
    assert item.phase == MessagePhase.COMMENTARY
    restored = NATIVE_ITEM_ADAPTER.validate_json(item.model_dump_json(exclude_unset=True))
    assert restored == item
    assert restored.content[0].annotations[0].title == "Reference"
    assert restored.content[0].logprobs[0].token == "Source"


def test_native_message_rejects_unmodeled_semantic_fields():
    with pytest.raises(ValidationError):
        NATIVE_ITEM_ADAPTER.validate_json(
            '{"type":"message","role":"assistant","content":[],"unmodeled_phase":"unknown"}'
        )


def test_historical_deepseek_usage_roundtrips_all_provider_counts():
    from agent_client.domain.models import ModelResponse

    response = ModelResponse.model_validate_json(
        '{"id":"old-deepseek","output":[],"usage":{"input_tokens":33,"output_tokens":8,"total_tokens":41,"prompt_tokens":33,"completion_tokens":8,"prompt_cache_hit_tokens":12,"prompt_cache_miss_tokens":21,"prompt_tokens_details":{"cached_tokens":12},"completion_tokens_details":{"reasoning_tokens":6},"input_tokens_details":{"cached_tokens":12}}}'
    )
    restored = ModelResponse.model_validate_json(response.model_dump_json())
    assert restored == response
    assert restored.usage.prompt_cache_miss_tokens == 21
    assert restored.usage.completion_tokens_details.reasoning_tokens == 6


def test_responses_usage_attribution_roundtrip_preserves_dynamic_wire_names():
    from agent_client.domain.protocol import TokenUsage

    wire = '{"input_tokens":40,"output_tokens":3,"total_tokens":43,"input_tokens_details":{"cached_tokens":10,"cache_write_tokens":5},"output_tokens_details":{"reasoning_tokens":1},"attribution":{"items":{"message-1":{"input_tokens":4,"output_tokens":3,"cached_tokens":0,"cache_write_tokens":0,"content":[{"input_tokens":4,"output_tokens":3,"cached_tokens":0,"cache_write_tokens":0}]},"call-1":{"input_tokens":5,"output_tokens":0,"cached_tokens":1,"cache_write_tokens":0}},"request_fields":{"instructions":{"input_tokens":31,"output_tokens":0,"cached_tokens":9,"cache_write_tokens":5}}}}'
    usage = TokenUsage.model_validate_json(wire)
    assert usage.input_tokens_details.cache_write_tokens == 5
    assert usage.attribution.items[1].name == "message-1"
    assert usage.attribution.items[0].usage.content is None
    dumped = usage.model_dump_json(exclude_unset=True)
    assert ProtocolObject(wire) == ProtocolObject(dumped)
    assert TokenUsage.model_validate_json(dumped) == usage


def test_attribution_rejects_unmodeled_counts_and_invalid_values():
    from agent_client.domain.protocol import TokenUsage

    with pytest.raises(ValidationError):
        TokenUsage.model_validate_json(
            '{"attribution":{"items":{"message-1":{"input_tokens":true,"output_tokens":0,"cached_tokens":0,"cache_write_tokens":0}},"request_fields":{}}}'
        )
