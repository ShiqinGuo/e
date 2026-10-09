from enum import StrEnum

from pydantic import (
    ConfigDict,
    Field,
    SecretStr,
    field_serializer,
    field_validator,
    model_validator,
)

from agent_client.domain.base import Contract
from agent_client.domain.enums import AuthMode


class AuthState(StrEnum):
    API_KEY_CONFIGURED = "api_key_configured"
    SIGNED_OUT = "signed_out"
    CONNECTED = "connected"
    REFRESHING = "refreshing"
    REAUTH_REQUIRED = "reauth_required"


class RevocationStatus(StrEnum):
    CONFIRMED = "confirmed"
    UNKNOWN = "unknown"
    NOT_NEEDED = "not_needed"


class OAuthGrant(StrEnum):
    AUTHORIZATION_CODE = "authorization_code"
    REFRESH_TOKEN = "refresh_token"


class UnusableRefreshToken(StrEnum):
    INVALID_GRANT = "invalid_grant"
    INVALID_REFRESH_TOKEN = "invalid_refresh_token"
    TOKEN_EXPIRED = "token_expired"
    REFRESH_TOKEN_EXPIRED = "refresh_token_expired"
    REFRESH_TOKEN_INVALIDATED = "refresh_token_invalidated"
    REFRESH_TOKEN_REUSED = "refresh_token_reused"


class OAuthFailure(Contract):
    model_config = ConfigDict(extra="ignore")
    error: str | None = None


class CallbackField(StrEnum):
    STATE = "state"
    CODE = "code"
    CLIENT_ID = "client_id"
    ERROR = "error"
    SCOPE = "scope"


class OAuthError(StrEnum):
    ACCESS_DENIED = "access_denied"
    INVALID_REQUEST = "invalid_request"
    UNAUTHORIZED_CLIENT = "unauthorized_client"
    UNSUPPORTED_RESPONSE_TYPE = "unsupported_response_type"
    INVALID_SCOPE = "invalid_scope"
    SERVER_ERROR = "server_error"
    TEMPORARILY_UNAVAILABLE = "temporarily_unavailable"


class BearerTokenType(StrEnum):
    BEARER = "bearer"


class HttpMethod(StrEnum):
    GET = "GET"
    POST = "POST"


class SigningAlgorithm(StrEnum):
    RS256 = "RS256"


class JwkKeyType(StrEnum):
    RSA = "RSA"
    EC = "EC"
    OKP = "OKP"
    OCT = "oct"


class AuthorizationResponseType(StrEnum):
    CODE = "code"


class ChallengeMethod(StrEnum):
    S256 = "S256"


class JwtHeader(Contract):
    model_config = ConfigDict(extra="ignore")
    kid: str
    alg: SigningAlgorithm


class AuthStatus(Contract):
    auth_mode: AuthMode | None = None
    api_key_env: str | None = None
    base_url: str | None = None
    status: AuthState
    subject: str | None = None
    email: str | None = None
    client_id: str | None = None
    expires_at: float | None = None
    token_generation: int | None = None
    revocation: RevocationStatus | None = None


class CatalogModel(Contract):
    slug: str = Field(min_length=1)
    display_name: str


class ApiCatalogItem(Contract):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(min_length=1)


class ApiCatalog(Contract):
    model_config = ConfigDict(extra="ignore")
    data: list[ApiCatalogItem]


class AuthRecord(Contract):
    status: AuthState
    host_id: str = Field(min_length=1)
    subject: str | None = None
    email: str | None = None
    client_id: str | None = None
    access_token: SecretStr | None = None
    refresh_token: SecretStr | None = None
    id_token: SecretStr | None = None
    scopes: list[str] = Field(default_factory=list)
    expires_at: float | None = None
    earliest_refresh_at: float | None = None
    token_generation: int = Field(default=0, ge=0)

    @field_serializer("access_token", "refresh_token", "id_token")
    def serialize_token(self, value: SecretStr | None) -> str | None:
        return value.get_secret_value() if value is not None else None

    @model_validator(mode="after")
    def validate_connected(self):
        if self.status == AuthState.CONNECTED and (
            self.subject is None
            or self.client_id is None
            or self.access_token is None
            or self.refresh_token is None
            or self.expires_at is None
        ):
            raise ValueError("Connected authentication requires complete credentials")
        return self


class IdentityClaims(Contract):
    model_config = ConfigDict(extra="ignore")
    iss: str
    aud: str | list[str]
    sub: str = Field(min_length=1)
    exp: float
    email: str | None = None
    nonce: str | None = None


class TokenResponse(Contract):
    model_config = ConfigDict(extra="ignore")
    access_token: SecretStr
    refresh_token: SecretStr
    id_token: SecretStr
    expires_in: float = Field(gt=0)
    scope: str
    token_type: BearerTokenType
    earliest_refresh_at: float | str | None = None

    @model_validator(mode="after")
    def validate_tokens(self):
        if any(
            not value.get_secret_value()
            for value in (self.access_token, self.refresh_token, self.id_token)
        ):
            raise ValueError("OAuth bearer tokens are required")
        return self

    @field_validator("token_type", mode="before")
    @classmethod
    def normalize_type(cls, value: str) -> str:
        if not isinstance(value, str):
            raise ValueError("OAuth token type must be a string")
        return value.casefold()


class AuthorizationCallback(Contract):
    state: SecretStr
    code: SecretStr | None = None
    client_id: str | None = None
    error: OAuthError | None = None
    scope: str | None = None


class Jwk(Contract):
    model_config = ConfigDict(extra="ignore")
    kid: str
    kty: JwkKeyType
    n: str | None = None
    e: str | None = None
    use: str | None = None
    alg: str | None = None


class Jwks(Contract):
    model_config = ConfigDict(extra="ignore")
    keys: list[Jwk]


class Discovery(Contract):
    model_config = ConfigDict(extra="ignore")
    revocation_endpoint: str


class CatalogVisibility(StrEnum):
    LIST = "list"
    HIDDEN = "hidden"


class CatalogEntry(Contract):
    model_config = ConfigDict(extra="ignore")
    slug: str
    display_name: str
    visibility: CatalogVisibility


class CatalogResponse(Contract):
    model_config = ConfigDict(extra="ignore")
    models: list[CatalogEntry]


class TokenRequest(Contract):
    grant_type: OAuthGrant
    client_id: str
    resource: str
    code: SecretStr | None = None
    code_verifier: SecretStr | None = None
    redirect_uri: str | None = None
    refresh_token: SecretStr | None = None

    @field_serializer("code", "code_verifier", "refresh_token")
    def serialize_secret(self, value: SecretStr | None) -> str | None:
        return value.get_secret_value() if value is not None else None

    @model_validator(mode="after")
    def validate_grant(self):
        match self.grant_type:
            case OAuthGrant.AUTHORIZATION_CODE:
                if self.code is None or self.code_verifier is None or self.redirect_uri is None:
                    raise ValueError("Authorization-code grant is incomplete")
            case OAuthGrant.REFRESH_TOKEN:
                if self.refresh_token is None:
                    raise ValueError("Refresh-token grant is incomplete")
        return self


class RevocationRequest(Contract):
    token: SecretStr
    token_type_hint: OAuthGrant = OAuthGrant.REFRESH_TOKEN
    client_id: str

    @field_serializer("token")
    def serialize_token(self, value: SecretStr) -> str:
        return value.get_secret_value()


class AuthorizationParameters(Contract):
    client_id: str
    ext_agent_host_id: str
    response_type: AuthorizationResponseType = AuthorizationResponseType.CODE
    redirect_uri: str
    scope: str
    resource: str
    state: SecretStr
    nonce: SecretStr
    code_challenge_method: ChallengeMethod = ChallengeMethod.S256
    code_challenge: str
    agent_name_hint: str | None = None
    id_token_hint: SecretStr | None = None

    @field_serializer("state", "nonce", "id_token_hint")
    def serialize_secret(self, value: SecretStr | None) -> str | None:
        return value.get_secret_value() if value is not None else None
