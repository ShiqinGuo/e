import base64
import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import TypedDict
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import SecretStr, ValidationError

from agent_client.domain.auth import (
    AuthRecord,
    AuthState,
    RevocationStatus,
    SigningAlgorithm,
    TokenResponse,
)
from agent_client.domain.enums import (
    ErrorCode,
)
from agent_client.domain.errors import AgentError
from agent_client.domain.provider import (
    ResponseEventType,
)
from agent_client.infrastructure.auth import AuthService
from agent_client.infrastructure.auth.service import ISSUER, SCOPES


class AuthFixture(TypedDict, total=False):
    status: AuthState
    host_id: str
    client_id: str
    subject: str
    access_token: str
    refresh_token: str
    expires_at: float
    scopes: list[str]
    token_generation: int


class IdentityFixtureContext(TypedDict, total=False):
    nonce: str
    params: dict[str, list[str]]
    form: dict[str, list[str]]


class LoginOutcome(StrEnum):
    DENIED = "denied"
    MISSING_REGISTRATION = "missing_registration"
    EXPIRED = "expired"


def auth_record(value: AuthFixture) -> AuthRecord:
    return AuthRecord.model_validate(
        {
            "host_id": "host",
            "client_id": "client",
            "subject": "subject",
            "access_token": "fixture-access",
            "refresh_token": "fixture-refresh",
            "expires_at": 0,
            **value,
        }
    )


def identity_server(
    *, bad_nonce: bool = False, missing_scope: bool = False
) -> tuple[
    Callable[[httpx.Request], httpx.Response],
    Callable[[str], Awaitable[None]],
    IdentityFixtureContext,
]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key())) | {"kid": "test"}
    context: IdentityFixtureContext = {}

    def handler(request):
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json={"revocation_endpoint": f"{ISSUER}/revoke"})
        if request.url.path.endswith("/revoke"):
            return httpx.Response(204)
        if request.url.path.endswith("jwks.json"):
            return httpx.Response(200, json={"keys": [jwk]})
        form = parse_qs(request.content.decode())
        context["form"] = form
        claims = {
            "iss": ISSUER,
            "aud": form["client_id"][0],
            "sub": "subject",
            "exp": int(time.time()) + 600,
            "nonce": "wrong" if bad_nonce else context["nonce"],
        }
        token = jwt.encode(claims, key, algorithm=SigningAlgorithm.RS256, headers={"kid": "test"})
        return httpx.Response(
            200,
            json={
                "id_token": token,
                "access_token": "secret-access",
                "refresh_token": "secret-refresh",
                "expires_in": 600,
                "scope": "openid" if missing_scope else " ".join(SCOPES),
                "token_type": "Bearer",
            },
        )

    async def on_url(url):
        params = parse_qs(urlsplit(url).query)
        context["nonce"] = params["nonce"][0]
        context["params"] = params
        async with httpx.AsyncClient() as local:
            callback = params["redirect_uri"][0]
            wrong = await local.get(callback, params={"state": "wrong", "code": "unused"})
            assert wrong.status_code == 400
            duplicate = await local.get(
                callback + "?" + urlencode({"state": params["state"][0]}) + "&code=a&code=b"
            )
            assert duplicate.status_code == 400
            result = await local.get(
                callback,
                params={"state": params["state"][0], "code": "code", "client_id": "oaiapp_test"},
            )
            assert result.status_code == 200
            replay = await local.get(callback, params={"state": params["state"][0], "code": "code"})
            assert replay.status_code == 400

    return (handler, on_url, context)


async def test_login_pkce_verified_identity_and_protected_store(tmp_path):
    handler, on_url, context = identity_server()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        auth = AuthService(tmp_path, client=client)
        result = await auth.login(on_url)
        assert result.subject == "subject" and result.client_id == "oaiapp_test"
        assert "secret" not in repr(result)
        verifier = context["form"]["code_verifier"][0]
        assert (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
            == context["params"]["code_challenge"][0]
        )
        assert context["form"]["redirect_uri"] == context["params"]["redirect_uri"]
        assert (await auth.access_token()).get_secret_value() == "secret-access"
        import os

        if os.name == "nt":
            assert b"secret-refresh" not in auth.store.path.read_bytes()
        else:
            assert auth.store.path.stat().st_mode & 511 == 384


@pytest.mark.parametrize("bad_nonce,missing_scope", [(True, False), (False, True)])
async def test_invalid_login_does_not_activate_account(tmp_path, bad_nonce, missing_scope):
    handler, on_url, _ = identity_server(bad_nonce=bad_nonce, missing_scope=missing_scope)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        auth = AuthService(tmp_path, client=client)
        with pytest.raises(AgentError):
            await auth.login(on_url)
        assert (await auth.status()).status == AuthState.SIGNED_OUT


async def test_reauthorization_cannot_replace_selected_subject(tmp_path):
    handler, on_url, _ = identity_server()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        auth = AuthService(tmp_path, client=client)
        original = {
            "status": AuthState.CONNECTED,
            "host_id": "host",
            "client_id": "oaiapp_test",
            "subject": "original",
            "access_token": "original-token",
            "refresh_token": "original-refresh",
            "expires_at": time.time() + 600,
        }
        await auth._write(auth_record(original))
        with pytest.raises(AgentError) as error:
            await auth.login(on_url)
        assert error.value.code == ErrorCode.ACCOUNT_MISMATCH
        assert await auth._read() == auth_record(original)


@pytest.mark.parametrize("outcome", list(LoginOutcome))
async def test_login_rejection_closes_loopback_without_exchange(tmp_path, outcome: LoginOutcome):
    requests = []
    callback_uri = None

    async def on_url(url):
        nonlocal callback_uri
        params = parse_qs(urlsplit(url).query)
        callback_uri = params["redirect_uri"][0]
        if outcome == LoginOutcome.EXPIRED:
            return
        callback = {"state": params["state"][0]}
        if outcome == LoginOutcome.DENIED:
            callback[ResponseEventType.ERROR] = "access_denied"
        else:
            callback["code"] = "code"
        async with httpx.AsyncClient() as local:
            assert (await local.get(callback_uri, params=callback)).status_code == 200

    def handler(request):
        requests.append(request)
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        auth = AuthService(
            tmp_path,
            client=client,
            authorization_timeout=0.05 if outcome == LoginOutcome.EXPIRED else 600,
        )
        with pytest.raises(AgentError):
            await auth.login(on_url)
        assert not requests
        async with httpx.AsyncClient(timeout=1) as local:
            with pytest.raises((httpx.ConnectError, httpx.ConnectTimeout)):
                await local.get(callback_uri)


async def test_refresh_unknown_not_retried_after_restart(tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        raise httpx.ReadTimeout("token secret-refresh", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        auth = AuthService(tmp_path, client=client)
        await auth._write(
            auth_record(
                {
                    "status": AuthState.CONNECTED,
                    "expires_at": 0,
                    "host_id": "host",
                    "client_id": "client",
                    "subject": "subject",
                    "refresh_token": "secret-refresh",
                    "token_generation": 1,
                }
            )
        )
        with pytest.raises(AgentError) as error:
            await auth.access_token()
        assert "secret" not in str(error.value)
        assert error.value.code == ErrorCode.AUTH_UNAVAILABLE
        assert "timed out" in str(error.value)
        restarted = AuthService(tmp_path, client=client)
        with pytest.raises(AgentError):
            await restarted.access_token()
        assert len(requests) == 1


@pytest.mark.parametrize("status_code", [400, 401, 500])
async def test_refresh_rejection_preserves_http_status_without_response_secrets(
    tmp_path, status_code
):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status_code, text="secret-refresh secret-access")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        auth = AuthService(tmp_path, client=client)
        await auth._write(auth_record({"status": AuthState.CONNECTED, "expires_at": 0}))
        with pytest.raises(AgentError) as error:
            await auth.access_token()
        assert error.value.code == ErrorCode.AUTH_UNAVAILABLE
        assert f"HTTP {status_code}" in str(error.value)
        assert "secret" not in str(error.value)
        assert (await auth.status()).status == AuthState.CONNECTED
        assert error.value.retryable == (status_code >= 500)
        assert len(requests) == 1


@pytest.mark.parametrize(
    "token_error",
    [
        "invalid_grant",
        "invalid_refresh_token",
        "token_expired",
        "refresh_token_expired",
        "refresh_token_invalidated",
        "refresh_token_reused",
    ],
)
async def test_terminal_refresh_error_clears_tokens_and_retains_registration(tmp_path, token_error):
    def handler(request):
        return httpx.Response(400, json={"error": token_error, "error_description": "secret"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        auth = AuthService(tmp_path, client=client)
        await auth._write(auth_record({"status": AuthState.CONNECTED, "expires_at": 0}))
        with pytest.raises(AgentError) as error:
            await auth.access_token()
        assert error.value.code == ErrorCode.REAUTH_REQUIRED
        assert token_error in str(error.value)
        assert "secret" not in str(error.value)
        stored = await auth._read()
        assert stored.status == AuthState.REAUTH_REQUIRED
        assert stored.client_id == "client" and stored.host_id == "host"
        assert (
            stored.access_token is None and stored.refresh_token is None and stored.id_token is None
        )


async def test_refresh_rotation_then_identity_failure_never_replays_old_token(tmp_path):
    token_requests = []

    def handler(request):
        if request.url.path.endswith("jwks.json"):
            return httpx.Response(503, json={"error": "temporarily_unavailable"})
        token_requests.append(request)
        return httpx.Response(
            200,
            json={
                "access_token": "replacement-access",
                "refresh_token": "replacement-refresh",
                "id_token": "replacement-identity",
                "expires_in": 3600,
                "scope": " ".join(SCOPES),
                "token_type": "Bearer",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        auth = AuthService(tmp_path, client=client)
        await auth._write(auth_record({"status": AuthState.CONNECTED, "expires_at": 0}))
        with pytest.raises(AgentError) as error:
            await auth.access_token()
        assert not error.value.retryable
        assert "HTTP 503" in str(error.value)
        assert (await auth.status()).status == AuthState.REAUTH_REQUIRED
        restarted = AuthService(tmp_path, client=client)
        with pytest.raises(AgentError):
            await restarted.access_token()
        assert len(token_requests) == 1


async def test_cancelled_refresh_and_waiting_owner_never_replay_token(tmp_path):
    import asyncio

    dispatched = asyncio.Event()
    stalled = asyncio.Event()
    calls = []

    async def handler(request):
        calls.append(request)
        dispatched.set()
        await stalled.wait()
        raise AssertionError("Cancelled request must not finish")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        first, second = (AuthService(tmp_path, client=client), AuthService(tmp_path, client=client))
        await first._write(
            auth_record(
                {
                    "status": AuthState.CONNECTED,
                    "expires_at": 0,
                    "host_id": "host",
                    "client_id": "client",
                    "subject": "subject",
                    "refresh_token": "old",
                    "token_generation": 1,
                }
            )
        )
        owner = asyncio.create_task(first.access_token())
        await dispatched.wait()
        waiter = asyncio.create_task(second.access_token())
        await asyncio.sleep(0.05)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        owner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await owner
        assert (await second.status()).status == AuthState.REAUTH_REQUIRED
        with pytest.raises(AgentError):
            await second.access_token()
        assert len(calls) == 1


async def test_cancel_during_atomic_credential_write_waits_before_releasing_owner(
    tmp_path, monkeypatch
):
    import asyncio
    import threading

    auth = AuthService(tmp_path)
    entered, release = (threading.Event(), threading.Event())
    actual_write = auth.store.write

    def slow_write(value):
        entered.set()
        release.wait(timeout=5)
        actual_write(value)

    monkeypatch.setattr(auth.store, "write", slow_write)

    async def operation():
        async with auth.store.locked():
            await auth._write(auth_record({"status": AuthState.REFRESHING, "token_generation": 1}))

    task = asyncio.create_task(operation())
    await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    await asyncio.sleep(0.05)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await auth.status()).status == AuthState.REAUTH_REQUIRED
    await auth.close()


async def test_durable_refresh_intent_blocks_crash_replay(tmp_path):
    auth = AuthService(tmp_path)
    await auth._write(auth_record({"status": AuthState.REFRESHING, "token_generation": 2}))
    assert (await auth.status()).status == AuthState.REAUTH_REQUIRED
    with pytest.raises(AgentError):
        await auth.access_token()
    await auth.close()


async def test_refresh_concurrent_instances_rotate_only_once(tmp_path):
    import asyncio

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key())) | {"kid": "test"}
    calls = []

    async def handler(request):
        if request.url.path.endswith("jwks.json"):
            return httpx.Response(200, json={"keys": [jwk]})
        calls.append(request)
        await asyncio.sleep(0.1)
        token = jwt.encode(
            {"iss": ISSUER, "aud": "client", "sub": "subject", "exp": time.time() + 600},
            key,
            algorithm=SigningAlgorithm.RS256,
            headers={"kid": "test"},
        )
        return httpx.Response(
            200,
            json={
                "id_token": token,
                "access_token": "new",
                "refresh_token": "rotated",
                "expires_in": 600,
                "scope": " ".join(SCOPES),
                "token_type": "Bearer",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        first, second = (AuthService(tmp_path, client=client), AuthService(tmp_path, client=client))
        await first._write(
            auth_record(
                {
                    "status": AuthState.CONNECTED,
                    "expires_at": 0,
                    "host_id": "host",
                    "client_id": "client",
                    "subject": "subject",
                    "refresh_token": "old",
                    "token_generation": 1,
                    "scopes": sorted(SCOPES),
                }
            )
        )
        assert [
            token.get_secret_value()
            for token in await asyncio.gather(first.access_token(), second.access_token())
        ] == ["new", "new"]
        assert len(calls) == 1
        assert (await first._read()).token_generation == 2


async def test_oidc_signature_audience_expiry_are_verified(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key())) | {"kid": "test"}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"keys": [jwk]}))
    ) as client:
        auth = AuthService(tmp_path, client=client)
        base = {"iss": ISSUER, "aud": "client", "sub": "subject", "exp": time.time() + 600}
        for signing_key, overrides in [
            (other_key, {}),
            (key, {"aud": "wrong"}),
            (key, {"exp": 1}),
            (key, {"iss": "wrong"}),
        ]:
            token = jwt.encode(
                base | overrides,
                signing_key,
                algorithm=SigningAlgorithm.RS256,
                headers={"kid": "test"},
            )
            with pytest.raises(AgentError) as error:
                await auth._identity(SecretStr(token), "client")
            assert token not in str(error.value)


async def test_logout_clears_local_tokens_even_when_revocation_unknown(tmp_path):

    def fail(request):
        raise httpx.ConnectError("failed", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
        auth = AuthService(tmp_path, client=client)
        await auth._write(
            auth_record(
                {
                    "host_id": "host",
                    "status": AuthState.CONNECTED,
                    "client_id": "client",
                    "refresh_token": "secret",
                }
            )
        )
        assert (await auth.logout()).revocation == RevocationStatus.UNKNOWN
        assert (await auth._read()).refresh_token is None
        with pytest.raises(AgentError):
            await auth.access_token()


@pytest.mark.parametrize("token_type", [None, 1, "unsupported"])
def test_invalid_token_type_is_a_validation_failure(token_type):
    with pytest.raises(ValidationError):
        TokenResponse(
            access_token="a",
            refresh_token="r",
            id_token="i",
            expires_in=60,
            scope="openid",
            token_type=token_type,
        )
