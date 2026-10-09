import pytest
from pydantic import ValidationError

from agent_client.config import load_config
from agent_client.domain.configuration import AppConfig, ContextConfig, McpServerConfig, ModelConfig
from agent_client.domain.enums import (
    AuthMode,
    ChatReasoningMode,
    McpTransport,
    ProviderKind,
    ReasoningEffort,
)


@pytest.mark.parametrize(
    "endpoint", ["http://localhost:invalid", "http://local host", "http://localhost:0"]
)
def test_invalid_endpoint_is_rejected_before_a_network_request(endpoint):
    with pytest.raises(ValidationError):
        ModelConfig(auth_mode=AuthMode.API_KEY, base_url=endpoint)
    with pytest.raises(ValidationError):
        McpServerConfig(transport=McpTransport.STREAMABLE_HTTP, url=endpoint)


def test_missing_explicit_config_is_not_silently_replaced_by_defaults(tmp_path):
    missing = tmp_path / "absent.toml"
    with pytest.raises(FileNotFoundError):
        load_config(missing)
    assert load_config(home=tmp_path).model.auth_mode == AuthMode.CHATGPT


def test_impossible_summary_budget_is_rejected_at_configuration_boundary():
    with pytest.raises(ValidationError, match="reserves"):
        AppConfig(context=ContextConfig(summary_max_output_tokens=256000))


def test_reasoning_levels_reject_unsupported_selection_without_mutation():
    config = ModelConfig(
        provider=ProviderKind.OPENAI_CHAT_COMPLETIONS,
        auth_mode=AuthMode.API_KEY,
        reasoning_effort=ReasoningEffort.HIGH,
        reasoning_levels=[ReasoningEffort.NONE, ReasoningEffort.HIGH, ReasoningEffort.MAX],
        chat_reasoning=ChatReasoningMode.ENABLED,
    )
    with pytest.raises(ValueError):
        config.select_reasoning(ReasoningEffort.MEDIUM)
    assert config.reasoning_effort == ReasoningEffort.HIGH
    config.select_reasoning(ReasoningEffort.NONE)
    assert config.chat_send_reasoning_effort
    config.select_reasoning(ReasoningEffort.MAX)
    assert config.reasoning_effort == ReasoningEffort.MAX


@pytest.mark.parametrize(
    "levels", [[], [ReasoningEffort.HIGH, ReasoningEffort.HIGH], [ReasoningEffort.LOW]]
)
def test_invalid_explicit_reasoning_levels_fail_at_configuration_boundary(levels):
    with pytest.raises(ValidationError):
        ModelConfig(reasoning_effort=ReasoningEffort.HIGH, reasoning_levels=levels)


@pytest.mark.parametrize("send_effort", [False, True])
def test_chat_none_without_thinking_switch_fails_at_construction(send_effort):
    with pytest.raises(ValidationError, match="explicit chat thinking switch"):
        ModelConfig(
            provider=ProviderKind.OPENAI_CHAT_COMPLETIONS,
            auth_mode=AuthMode.API_KEY,
            reasoning_effort=ReasoningEffort.NONE,
            chat_reasoning=ChatReasoningMode.DEFAULT,
            chat_send_reasoning_effort=send_effort,
        )


@pytest.mark.parametrize("thinking", [ChatReasoningMode.ENABLED, ChatReasoningMode.DISABLED])
def test_chat_none_with_explicit_thinking_switch_is_valid(thinking):
    model = ModelConfig(
        provider=ProviderKind.OPENAI_CHAT_COMPLETIONS,
        auth_mode=AuthMode.API_KEY,
        reasoning_effort=ReasoningEffort.NONE,
        chat_reasoning=thinking,
    )
    assert model.reasoning_effort == ReasoningEffort.NONE


def test_responses_summary_defaults_to_low_without_model_name_assumptions():
    config = AppConfig(model=ModelConfig(model="custom-reasoning-model"))
    assert config.context.summary_reasoning_effort is None
    assert config.summary_effort() == ReasoningEffort.LOW


@pytest.mark.parametrize("effort", [ReasoningEffort.NONE, ReasoningEffort.HIGH])
def test_explicit_summary_effort_is_preserved(effort):
    config = AppConfig(context=ContextConfig(summary_reasoning_effort=effort))
    assert config.summary_effort() == effort


@pytest.mark.parametrize("thinking", list(ChatReasoningMode))
def test_chat_summary_effort_respects_explicit_thinking_capability(thinking):
    config = AppConfig(
        model=ModelConfig(
            provider=ProviderKind.OPENAI_CHAT_COMPLETIONS,
            auth_mode=AuthMode.API_KEY,
            reasoning_effort=ReasoningEffort.HIGH,
            chat_reasoning=thinking,
        )
    )
    expected = (
        ReasoningEffort.HIGH if thinking == ChatReasoningMode.DEFAULT else ReasoningEffort.NONE
    )
    assert config.summary_effort() == expected
