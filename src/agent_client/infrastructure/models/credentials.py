import os

from pydantic import SecretStr

from agent_client.domain.configuration import ModelConfig
from agent_client.domain.enums import ErrorCode
from agent_client.domain.errors import AgentError


def api_key(config: ModelConfig) -> SecretStr:
    value = os.environ[config.api_key_env] if config.api_key_env in os.environ else None
    if value is None or not value.strip():
        raise AgentError(
            ErrorCode.API_KEY_MISSING, f"Set the {config.api_key_env} environment variable"
        )
    return SecretStr(value)
