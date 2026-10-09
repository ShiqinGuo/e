import asyncio
import base64
import hashlib
import secrets
import time
import webbrowser
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TypedDict
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx
import jwt
from pydantic import BaseModel, SecretStr

from agent_client.domain.auth import (
    AuthorizationCallback,
    AuthorizationParameters,
    AuthRecord,
    AuthState,
    AuthStatus,
    CallbackField,
    CatalogModel,
    CatalogResponse,
    CatalogVisibility,
    Discovery,
    HttpMethod,
    IdentityClaims,
    Jwks,
    JwtHeader,
    OAuthError,
    OAuthFailure,
    OAuthGrant,
    RevocationRequest,
    RevocationStatus,
    SigningAlgorithm,
    TokenRequest,
    TokenResponse,
    UnusableRefreshToken,
)
from agent_client.domain.enums import ErrorCode
from agent_client.domain.errors import AgentError
from agent_client.infrastructure.auth.store import CredentialStore

ISSUER = "https://auth.openai.com"
RESOURCE = "https://api.openai.com/v1"
SCOPES = frozenset({"openid", "offline_access", "resource.invoke", "chatgpt.tokens.use.direct"})
DYNAMIC_CLIENT_ID = "dynamic_agent_client"


@dataclass(frozen=True)
class AuthLimits:
    maximum_authorization_seconds: float = 600
    http_timeout_seconds: float = 30
    callback_timeout_seconds: float = 10
    callback_header_bytes: int = 16384
    nonce_bytes: int = 48
    expiry_margin_seconds: float = 30


class AuthorizationHeaders(TypedDict):
    Authorization: str


class JwtVerificationOptions(TypedDict):
    require: list[str]


class AuthRequestFailure(AgentError):
    def __init__(self, status: int, token_error: UnusableRefreshToken | None):
        code = ErrorCode.REAUTH_REQUIRED if token_error is not None else ErrorCode.AUTH_UNAVAILABLE
        detail = f", code {token_error.value}" if token_error is not None else ""
        super().__init__(
            code,
            f"ChatGPT authorization request failed (HTTP {status}{detail})",
            retryable=status >= 500,
        )
        self.http_status = status
        self.token_error = token_error


class AuthService:
    def __init__(
        self,
        home: Path,
        *,
        client: httpx.AsyncClient | None = None,
        authorization_timeout: float = 600,
    ):
        if not 0 < authorization_timeout <= AuthLimits().maximum_authorization_seconds:
            raise ValueError("Authorization timeout must be within 600 seconds")
        self.store = CredentialStore(home)
        self.client = client or httpx.AsyncClient(
            timeout=AuthLimits().http_timeout_seconds, follow_redirects=False
        )
        self._owns_client = client is None
        self.authorization_timeout = authorization_timeout

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def _read(self) -> AuthRecord | None:
        return await asyncio.to_thread(self.store.read)

    async def _write(self, value: AuthRecord) -> None:
        operation = asyncio.create_task(asyncio.to_thread(self.store.write, value))
        cancelled = False
        while not operation.done():
            try:
                await asyncio.shield(operation)
            except asyncio.CancelledError:
                cancelled = True
        operation.result()
        if cancelled:
            raise asyncio.CancelledError

    @staticmethod
    def _public(value: AuthRecord | None) -> AuthStatus:
        if value is None:
            return AuthStatus(status=AuthState.SIGNED_OUT)
        return AuthStatus(
            status=value.status,
            subject=value.subject,
            email=value.email,
            client_id=value.client_id,
            expires_at=value.expires_at,
            token_generation=value.token_generation,
        )

    async def status(self) -> AuthStatus:
        async with self.store.locked():
            value = await self._read()
            if value is not None and value.status == AuthState.REFRESHING:
                value.status = AuthState.REAUTH_REQUIRED
                await self._write(value)
            return self._public(value)

    async def _request[T: BaseModel](
        self,
        method: HttpMethod,
        url: str,
        response_type: type[T],
        *,
        form: TokenRequest | None = None,
        token: SecretStr | None = None,
    ) -> T:
        try:
            response = await self.client.request(
                method.value,
                url,
                follow_redirects=False,
                data=form.model_dump(mode="json", exclude_none=True) if form is not None else None,
                headers=AuthorizationHeaders(Authorization=f"Bearer {token.get_secret_value()}")
                if token is not None
                else None,
            )
            if not response.is_success:
                try:
                    failure = OAuthFailure.model_validate(response.json())
                except ValueError:
                    raise AuthRequestFailure(response.status_code, None) from None
                token_error = (
                    UnusableRefreshToken(failure.error)
                    if failure.error in UnusableRefreshToken
                    else None
                )
                raise AuthRequestFailure(response.status_code, token_error)
            return response_type.model_validate(response.json())
        except httpx.TimeoutException:
            raise AgentError(
                ErrorCode.AUTH_UNAVAILABLE, "Authorization request timed out"
            ) from None
        except httpx.ConnectError:
            raise AgentError(
                ErrorCode.AUTH_UNAVAILABLE, "Authorization transport failed", retryable=True
            ) from None
        except httpx.HTTPError:
            raise AgentError(
                ErrorCode.AUTH_UNAVAILABLE, "Authorization transport outcome is unknown"
            ) from None
        except ValueError:
            raise AgentError(
                ErrorCode.AUTH_UNAVAILABLE, "Authorization result could not be verified"
            ) from None

    async def _identity(
        self, token: SecretStr, client_id: str, nonce: SecretStr | None = None
    ) -> IdentityClaims:
        try:
            keys = await self._request(HttpMethod.GET, f"{ISSUER}/.well-known/jwks.json", Jwks)
            header = JwtHeader.model_validate(jwt.get_unverified_header(token.get_secret_value()))
            key = next(key for key in keys.keys if key.kid == header.kid)
            claims = IdentityClaims.model_validate(
                jwt.decode(
                    token.get_secret_value(),
                    jwt.PyJWK.from_dict(key.model_dump()).key,
                    algorithms=[SigningAlgorithm.RS256.value],
                    audience=client_id,
                    issuer=ISSUER,
                    options=JwtVerificationOptions(require=["exp", "iss", "aud", "sub"]),
                )
            )
            if nonce is not None and not secrets.compare_digest(
                claims.nonce or "", nonce.get_secret_value()
            ):
                raise ValueError
            return claims
        except (jwt.PyJWTError, StopIteration, ValueError, TypeError):
            raise AgentError(
                ErrorCode.IDENTITY_INVALID, "ChatGPT identity validation failed"
            ) from None

    async def _credentials(
        self,
        data: TokenResponse,
        previous: AuthRecord,
        client_id: str,
        nonce: SecretStr | None = None,
    ) -> AuthRecord:
        claims = await self._identity(data.id_token, client_id, nonce)
        granted = set(data.scope.split())
        if not SCOPES.issubset(granted):
            raise AgentError(ErrorCode.SCOPE_MISSING, "ChatGPT plan usage permission is required")
        if previous.subject is not None and claims.sub != previous.subject:
            raise AgentError(
                ErrorCode.ACCOUNT_MISMATCH, "Authorization selected a different account"
            )
        if previous.scopes and granted != set(previous.scopes):
            raise AgentError(
                ErrorCode.SCOPE_CHANGED, "Authorization permissions changed; sign in again"
            )
        earliest = data.earliest_refresh_at
        if isinstance(earliest, str):
            try:
                earliest = datetime.fromisoformat(earliest.replace("Z", "+00:00")).timestamp()
            except ValueError:
                raise AgentError(
                    ErrorCode.IDENTITY_INVALID, "Authorization refresh time is invalid"
                ) from None
        return AuthRecord(
            status=AuthState.CONNECTED,
            host_id=previous.host_id,
            client_id=client_id,
            subject=claims.sub,
            email=claims.email,
            access_token=data.access_token,
            refresh_token=data.refresh_token,
            id_token=data.id_token,
            scopes=sorted(granted),
            expires_at=time.time() + data.expires_in,
            earliest_refresh_at=earliest,
            token_generation=previous.token_generation + 1,
        )

    async def login(
        self, on_url: Callable[[str], Awaitable[None]] | None = None, *, new_account: bool = False
    ) -> AuthStatus:
        async with self.store.locked():
            saved = await self._read()
            previous = (
                saved
                if saved is not None and not new_account
                else AuthRecord(
                    host_id=await asyncio.to_thread(self.store.host_id), status=AuthState.SIGNED_OUT
                )
            )
            if saved is None:
                await self._write(previous)
            state, nonce, verifier = (
                SecretStr(secrets.token_urlsafe(AuthLimits().nonce_bytes)) for _ in range(3)
            )
            callback: asyncio.Future[AuthorizationCallback] = (
                asyncio.get_running_loop().create_future()
            )
            deadline = asyncio.get_running_loop().time() + self.authorization_timeout
            connections: set[asyncio.Task] = set()

            async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
                task = asyncio.current_task()
                if task is not None:
                    connections.add(task)
                try:
                    raw = await asyncio.wait_for(
                        reader.readuntil(b"\r\n\r\n"), AuthLimits().callback_timeout_seconds
                    )
                    method, target, _ = raw.decode("ascii").split("\r\n", 1)[0].split(" ")
                    parsed = urlsplit(target)
                    values = parse_qsl(parsed.query, strict_parsing=True, keep_blank_values=True)
                    names = [name for name, _ in values]
                    result = AuthorizationCallback(state=SecretStr(""))
                    for name, value in values:
                        match name:
                            case CallbackField.STATE:
                                result.state = SecretStr(value)
                            case CallbackField.CODE:
                                result.code = SecretStr(value)
                            case CallbackField.CLIENT_ID:
                                result.client_id = value
                            case CallbackField.ERROR:
                                result.error = OAuthError(value)
                            case CallbackField.SCOPE:
                                result.scope = value
                            case _:
                                raise ValueError("Unknown authorization callback field")
                    valid = (
                        len(set(names)) == len(names)
                        and method == HttpMethod.GET.value
                        and parsed.path == "/auth/callback"
                        and not parsed.netloc
                        and secrets.compare_digest(
                            result.state.get_secret_value(), state.get_secret_value()
                        )
                        and not callback.done()
                        and asyncio.get_running_loop().time() < deadline
                    )
                    if valid:
                        callback.set_result(result)
                    writer.write(
                        b"HTTP/1.1 "
                        + (b"200 OK" if valid else b"400 Bad Request")
                        + b"\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                    )
                    await writer.drain()
                except (
                    ValueError,
                    UnicodeError,
                    TimeoutError,
                    asyncio.IncompleteReadError,
                    asyncio.LimitOverrunError,
                    ConnectionError,
                ):
                    pass
                finally:
                    writer.close()
                    try:
                        await writer.wait_closed()
                    except ConnectionError:
                        pass
                    if task is not None:
                        connections.discard(task)

            server = await asyncio.start_server(
                handle, "127.0.0.1", 0, limit=AuthLimits().callback_header_bytes
            )
            redirect = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/auth/callback"
            client_id = previous.client_id or DYNAMIC_CLIENT_ID
            params = AuthorizationParameters(
                client_id=client_id,
                ext_agent_host_id=previous.host_id,
                redirect_uri=redirect,
                scope="openid profile email offline_access resource.invoke chatgpt.tokens.use.direct",
                resource=RESOURCE,
                state=state,
                nonce=nonce,
                code_challenge=base64.urlsafe_b64encode(
                    hashlib.sha256(verifier.get_secret_value().encode()).digest()
                )
                .rstrip(b"=")
                .decode(),
                agent_name_hint="Agent Client" if client_id == DYNAMIC_CLIENT_ID else None,
                id_token_hint=previous.id_token,
            )
            try:
                url = f"{ISSUER}/api/accounts/authorize?{urlencode(params.model_dump(mode='json', exclude_none=True))}"
                if on_url is not None:
                    await asyncio.wait_for(on_url(url), self.authorization_timeout)
                elif not await asyncio.to_thread(webbrowser.open, url):
                    raise AgentError(
                        ErrorCode.BROWSER_UNAVAILABLE, "Unable to open authorization browser"
                    )
                result = await asyncio.wait_for(
                    callback, max(0, deadline - asyncio.get_running_loop().time())
                )
                if result.error is not None:
                    raise AgentError(ErrorCode.AUTH_DENIED, "ChatGPT authorization was denied")
                issued = result.client_id or client_id
                if issued == DYNAMIC_CLIENT_ID or (
                    client_id != DYNAMIC_CLIENT_ID and issued != client_id
                ):
                    raise AgentError(
                        ErrorCode.REGISTRATION_INVALID, "Authorization registration does not match"
                    )
                if result.code is None:
                    raise AgentError(ErrorCode.AUTH_INVALID, "Authorization callback is incomplete")
                data = await self._request(
                    HttpMethod.POST,
                    f"{ISSUER}/api/accounts/oauth/token",
                    TokenResponse,
                    form=TokenRequest(
                        grant_type=OAuthGrant.AUTHORIZATION_CODE,
                        client_id=issued,
                        code=result.code,
                        code_verifier=verifier,
                        redirect_uri=redirect,
                        resource=RESOURCE,
                    ),
                )
                try:
                    value = await self._credentials(data, previous, issued, nonce)
                except AgentError:
                    await self._revoke(data.refresh_token, issued)
                    raise
                await self._write(value)
                return self._public(value)
            except TimeoutError:
                raise AgentError(
                    ErrorCode.AUTH_TIMEOUT, "ChatGPT authorization timed out"
                ) from None
            finally:
                server.close()
                await server.wait_closed()
                for task in list(connections):
                    task.cancel()
                if connections:
                    await asyncio.gather(*connections, return_exceptions=True)

    async def access_token(self) -> SecretStr:
        async with self.store.locked():
            value = await self._read()
            if value is None or value.status != AuthState.CONNECTED:
                if value is not None and value.status == AuthState.REFRESHING:
                    value.status = AuthState.REAUTH_REQUIRED
                    await self._write(value)
                raise AgentError(ErrorCode.REAUTH_REQUIRED, "Sign in with ChatGPT to continue")
            if value.expires_at > time.time() + AuthLimits().expiry_margin_seconds:
                return value.access_token
            if (value.earliest_refresh_at or 0) > time.time():
                raise AgentError(
                    ErrorCode.REFRESH_NOT_READY, "Authorization refresh is not yet allowed"
                )
            value.status = AuthState.REFRESHING
            await self._write(value)
            token_replaced = False
            try:
                data = await self._request(
                    HttpMethod.POST,
                    f"{ISSUER}/api/accounts/oauth/token",
                    TokenResponse,
                    form=TokenRequest(
                        grant_type=OAuthGrant.REFRESH_TOKEN,
                        client_id=value.client_id,
                        refresh_token=value.refresh_token,
                        resource=RESOURCE,
                    ),
                )
                token_replaced = True
                updated = await self._credentials(data, value, value.client_id)
                await self._write(updated)
                return updated.access_token
            except AgentError as error:
                preserve_connection = not token_replaced and (
                    error.retryable
                    or (isinstance(error, AuthRequestFailure) and error.token_error is None)
                )
                value.status = (
                    AuthState.CONNECTED if preserve_connection else AuthState.REAUTH_REQUIRED
                )
                if isinstance(error, AuthRequestFailure) and error.token_error is not None:
                    value.access_token = None
                    value.refresh_token = None
                    value.id_token = None
                await self._write(value)
                raise AgentError(
                    error.code,
                    f"Refresh failed: {error.message}"
                    + ("; credentials preserved" if preserve_connection else "; sign in again"),
                    retryable=error.retryable and preserve_connection,
                ) from None
            except OSError:
                value.status = AuthState.REAUTH_REQUIRED
                await self._write(value)
                raise AgentError(
                    ErrorCode.REAUTH_REQUIRED,
                    "Refresh credential persistence failed; sign in again",
                ) from None

    async def models(self) -> list[CatalogModel]:
        token = await self.access_token()
        data = await self._request(
            HttpMethod.GET, f"{RESOURCE}/models", CatalogResponse, token=token
        )
        return [
            CatalogModel(slug=m.slug, display_name=m.display_name)
            for m in data.models
            if m.visibility == CatalogVisibility.LIST
        ]

    async def logout(self) -> AuthStatus:
        async with self.store.locked():
            value = await self._read()
            if value is None:
                return AuthStatus(
                    status=AuthState.SIGNED_OUT, revocation=RevocationStatus.NOT_NEEDED
                )
            public = AuthRecord(
                status=AuthState.SIGNED_OUT,
                host_id=value.host_id,
                client_id=value.client_id,
                subject=value.subject,
                email=value.email,
            )
            await self._write(public)
            outcome = (
                await self._revoke(value.refresh_token, value.client_id)
                if value.refresh_token is not None
                else RevocationStatus.NOT_NEEDED
            )
            return AuthStatus(status=AuthState.SIGNED_OUT, revocation=outcome)

    async def _revoke(self, token: SecretStr, client_id: str) -> RevocationStatus:
        try:
            discovery = await self._request(
                HttpMethod.GET, f"{ISSUER}/.well-known/openid-configuration", Discovery
            )
            endpoint = discovery.revocation_endpoint
            if not endpoint.startswith(f"{ISSUER}/"):
                return RevocationStatus.UNKNOWN
            payload = RevocationRequest(token=token, client_id=client_id)
            response = await self.client.post(
                endpoint, follow_redirects=False, data=payload.model_dump(mode="json")
            )
            return RevocationStatus.CONFIRMED if response.is_success else RevocationStatus.UNKNOWN
        except (AgentError, httpx.HTTPError, ValueError):
            return RevocationStatus.UNKNOWN
