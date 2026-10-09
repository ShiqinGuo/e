from http import HTTPStatus
from typing import TypedDict

import httpx
from pydantic import ValidationError

from agent_client.domain.auth import ApiCatalog, AuthState, AuthStatus, CatalogModel
from agent_client.domain.configuration import ModelConfig
from agent_client.domain.enums import AuthMode, ErrorCode
from agent_client.domain.errors import AgentError
from agent_client.infrastructure.auth import AuthService
from agent_client.infrastructure.models.credentials import api_key


class AuthorizationHeaders(TypedDict):
    Authorization: str


class ProviderAccess:
    def __init__(self, config: ModelConfig, auth: AuthService):
        self.config = config
        self.auth = auth

    async def status(self) -> AuthStatus:
        if self.config.auth_mode == AuthMode.CHATGPT:
            return await self.auth.status()
        try:
            api_key(self.config)
            state = AuthState.API_KEY_CONFIGURED
        except AgentError:
            state = AuthState.SIGNED_OUT
        return AuthStatus(
            status=state,
            auth_mode=AuthMode.API_KEY,
            api_key_env=self.config.api_key_env,
            base_url=self.config.base_url,
        )

    async def login(self, *, new_account: bool = False) -> AuthStatus:
        if self.config.auth_mode == AuthMode.CHATGPT:
            return await self.auth.login(new_account=new_account)
        if new_account:
            raise AgentError(ErrorCode.CONFIG_INVALID, "API key mode does not use browser accounts")
        await self.models()
        result = await self.status()
        result.status = AuthState.CONNECTED
        return result

    async def logout(self) -> AuthStatus:
        if self.config.auth_mode == AuthMode.CHATGPT:
            return await self.auth.logout()
        raise AgentError(
            ErrorCode.CONFIG_INVALID,
            f"API key authentication comes from {self.config.api_key_env}; remove that variable or switch configuration to disconnect",
        )

    async def models(self) -> list[CatalogModel]:
        if self.config.auth_mode == AuthMode.CHATGPT:
            return await self.auth.models()
        token = api_key(self.config)
        try:
            response = await self.auth.client.get(
                self.config.base_url.rstrip("/") + "/models",
                headers=AuthorizationHeaders(Authorization=f"Bearer {token.get_secret_value()}"),
                follow_redirects=False,
            )
            match response.status_code:
                case HTTPStatus.OK:
                    catalog = ApiCatalog.model_validate(response.json())
                    return [
                        CatalogModel(slug=item.id, display_name=item.id) for item in catalog.data
                    ]
                case HTTPStatus.UNAUTHORIZED | HTTPStatus.FORBIDDEN:
                    raise AgentError(
                        ErrorCode.MODEL_ACCESS_DENIED,
                        "The API provider rejected the configured key",
                    )
                case _:
                    raise AgentError(
                        ErrorCode.MODEL_CATALOG_INVALID,
                        f"The API provider returned HTTP {response.status_code} for /models",
                    )
        except (httpx.HTTPError, ValidationError, ValueError):
            raise AgentError(
                ErrorCode.MODEL_CATALOG_INVALID,
                "The API provider model catalog could not be verified",
            ) from None
