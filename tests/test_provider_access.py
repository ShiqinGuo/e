import httpx
import pytest
from pydantic import ValidationError

from agent_client.domain.auth import AuthState
from agent_client.domain.configuration import ModelConfig
from agent_client.domain.enums import AuthMode, ErrorCode, ProviderKind
from agent_client.domain.errors import AgentError
from agent_client.infrastructure.models.access import ProviderAccess


class SubscriptionMustNotBeUsed:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def status(self):
        raise AssertionError("Subscription status must not be read")

    async def login(self, *, new_account=False):
        raise AssertionError("Subscription browser login must not be started")

    async def logout(self):
        raise AssertionError("Subscription credentials must not be changed")

    async def models(self):
        raise AssertionError("Subscription catalog must not be read")


def configuration() -> ModelConfig:
    return ModelConfig(
        provider=ProviderKind.OPENAI_CHAT_COMPLETIONS,
        auth_mode=AuthMode.API_KEY,
        api_key_env="TEST_PROVIDER_ACCESS_KEY",
        base_url="https://provider.test/v1",
        model="test-model",
    )


async def test_api_key_status_and_login_use_only_configured_provider(monkeypatch):
    secret = "offline-provider-key"
    monkeypatch.setenv("TEST_PROVIDER_ACCESS_KEY", secret)
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request):
        requests.append(request)
        assert str(request.url) == "https://provider.test/v1/models"
        assert request.method == "GET"
        assert request.headers["Authorization"] == f"Bearer {secret}"
        assert "ChatGPT-Account-Id" not in request.headers
        return httpx.Response(200, json={"data": [{"id": "test-model"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        access = ProviderAccess(configuration(), SubscriptionMustNotBeUsed(client))
        status = await access.status()
        assert status.status == AuthState.API_KEY_CONFIGURED
        assert requests == []
        connected = await access.login()
        assert connected.status == AuthState.CONNECTED
        assert len(requests) == 1
        assert secret not in connected.model_dump_json()
        with pytest.raises(AgentError) as account_failure:
            await access.login(new_account=True)
        assert account_failure.value.code == ErrorCode.CONFIG_INVALID
        assert len(requests) == 1
        with pytest.raises(AgentError) as failure:
            await access.logout()
        assert failure.value.code == ErrorCode.CONFIG_INVALID
        assert secret not in str(failure.value)
        assert len(requests) == 1


async def test_missing_api_key_status_and_login_never_access_subscription(monkeypatch):
    monkeypatch.delenv("TEST_PROVIDER_ACCESS_KEY", raising=False)

    def handle(request):
        raise AssertionError("Missing credentials must fail before network access")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        access = ProviderAccess(configuration(), SubscriptionMustNotBeUsed(client))
        assert (await access.status()).status == AuthState.SIGNED_OUT
        with pytest.raises(AgentError) as failure:
            await access.login()
        assert failure.value.code == ErrorCode.API_KEY_MISSING


@pytest.mark.parametrize("status_code", [401, 403])
async def test_api_key_catalog_rejection_is_explicit_without_secret(monkeypatch, status_code):
    secret = "offline-rejected-key"
    monkeypatch.setenv("TEST_PROVIDER_ACCESS_KEY", secret)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(status_code, json={"error": {"message": secret}})
        )
    ) as client:
        access = ProviderAccess(configuration(), SubscriptionMustNotBeUsed(client))
        with pytest.raises(AgentError) as failure:
            await access.login()
        assert failure.value.code == ErrorCode.MODEL_ACCESS_DENIED
        assert secret not in str(failure.value)


@pytest.mark.parametrize("endpoint", ["https://api.openai.com/v1", "https://provider.test/v1"])
def test_chat_completions_rejects_subscription_authentication(endpoint):
    with pytest.raises(ValidationError):
        ModelConfig(
            provider=ProviderKind.OPENAI_CHAT_COMPLETIONS,
            auth_mode=AuthMode.CHATGPT,
            base_url=endpoint,
        )
