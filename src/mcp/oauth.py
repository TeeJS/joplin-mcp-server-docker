"""OAuth 2.1 resource server for the Joplin MCP endpoint.

Implements the protected-resource half of the MCP authorization spec: validates
bearer JWTs issued by an external OIDC provider, publishes the discovery
documents clients use to find that provider, and resolves the caller's identity
provider groups into a read/write permission.

Off by default. The same image runs unauthenticated on a trusted LAN and gated
on a public hostname, so the posture is a deployment decision rather than a
build-time one.

Nothing here issues tokens. PKCE, 2FA policy, token lifespans, and refresh are
all authorization-server concerns; a resource server never sees them.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

import httpx
import jwt
from jwt import PyJWKClient
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

logger = logging.getLogger(__name__)

# Unauthenticated paths. Discovery has to stay reachable or the flow is
# undiscoverable, and health has to stay reachable or enabling auth silently
# breaks the container healthcheck.
PROTECTED_RESOURCE_PATH = "/.well-known/oauth-protected-resource"
AUTHORIZATION_SERVER_PATH = "/.well-known/oauth-authorization-server"
HEALTH_PATH = "/healthz"

# What this server needs: an identity, the group membership tool access is
# derived from, and a refresh token. Declared in the resource metadata *and* on
# the 401 challenge, because a client that is not told which scopes to request
# falls back to requesting every scope the provider advertises — and a provider
# that rejects rather than ignores an unpermitted scope fails the whole
# authorization request, with nothing useful surfacing on the client side.
REQUIRED_SCOPES = ("openid", "profile", "groups", "offline_access")

# Permission levels. Plain strings so they survive the trip through the ASGI
# scope without importing this module at the tool layer.
PERMISSION_NONE = "none"
PERMISSION_READ = "read"
PERMISSION_WRITE = "write"

# Key the middleware stashes the resolved permission under, on the ASGI scope.
# A contextvar would not survive: the MCP request handler runs in a different
# task than the HTTP middleware.
SCOPE_PERMISSION_KEY = "mcp_permission"

DISCOVERY_TIMEOUT_SECONDS = 10.0


def _env_str(name: str, default: str = "") -> str:
    """Read an environment variable, trimmed.

    Pasted by hand into container UIs, so a leading tab is routine — and left in
    place it produces an unusable discovery URL and an error pointing nowhere
    near the real mistake.
    """
    return os.environ.get(name, default).strip() or default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env_str(name, "").lower()
    if not raw:
        return default

    return raw in {"1", "true", "yes", "on"}


def _env_list(name: str) -> tuple[str, ...]:
    """Parse a comma-separated list, dropping blanks."""
    return tuple(
        item.strip() for item in _env_str(name, "").split(",") if item.strip()
    )


class OAuthConfigError(Exception):
    """OAuth is enabled but the configuration cannot be used."""


@dataclass(frozen=True)
class GroupPolicy:
    """Maps identity provider groups onto permission levels.

    Both lists empty disables group checking entirely, which is what keeps the
    zero-config LAN case working. Once either is set, a caller in neither is
    refused.
    """

    claim: str = "groups"
    read_groups: tuple[str, ...] = ()
    write_groups: tuple[str, ...] = ()

    @property
    def configured(self) -> bool:
        return bool(self.read_groups or self.write_groups)

    def evaluate(self, groups: Iterable[str]) -> str:
        """Resolve groups to a permission. Write wins when a caller has both."""
        if not self.configured:
            return PERMISSION_WRITE

        held = set(groups)

        if held.intersection(self.write_groups):
            return PERMISSION_WRITE

        if held.intersection(self.read_groups):
            return PERMISSION_READ

        return PERMISSION_NONE


def groups_from_claims(claims: dict[str, Any], claim: str) -> list[str]:
    """Extract a group list from a claim.

    Providers are inconsistent: some emit a JSON array, some a single string,
    some a space-delimited string. Handle all three rather than discovering
    which one this provider uses through a failed deployment.
    """
    raw = claims.get(claim)

    if isinstance(raw, str):
        return raw.split()

    if isinstance(raw, (list, tuple)):
        return [item for item in raw if isinstance(item, str)]

    return []


@dataclass(frozen=True)
class OAuthConfig:
    """Resource server configuration, already normalized."""

    enabled: bool = False
    issuer: str = ""
    server_url: str = ""
    mcp_path: str = "/mcp"
    audience: str = ""
    jwks_cache_ttl: int = 3600
    groups: GroupPolicy = field(default_factory=GroupPolicy)

    @classmethod
    def from_env(cls) -> "OAuthConfig":
        return cls(
            enabled=_env_bool("MCP_OAUTH_ENABLED", False),
            issuer=_env_str("MCP_OAUTH_ISSUER").rstrip("/"),
            server_url=_env_str("MCP_SERVER_URL").rstrip("/"),
            mcp_path="/" + _env_str("MCP_PATH", "/mcp").strip("/"),
            audience=_env_str("MCP_OAUTH_AUDIENCE"),
            jwks_cache_ttl=int(_env_str("MCP_OAUTH_JWKS_CACHE_TTL", "3600")),
            groups=GroupPolicy(
                # The skill's canonical name is MCP_OAUTH_GROUPS_CLAIM; the
                # sibling Go server shipped MCP_GROUPS_CLAIM first. Accept both
                # so one deployment pattern covers every MCP server here.
                claim=_env_str("MCP_OAUTH_GROUPS_CLAIM")
                or _env_str("MCP_GROUPS_CLAIM", "groups"),
                read_groups=_env_list("MCP_READ_GROUPS"),
                write_groups=_env_list("MCP_WRITE_GROUPS"),
            ),
        )

    def validate(self) -> None:
        """Refuse to start misconfigured rather than serving an open endpoint."""
        if not self.enabled:
            return

        if not self.issuer:
            raise OAuthConfigError(
                "MCP_OAUTH_ENABLED is true but MCP_OAUTH_ISSUER is not set"
            )

        if not self.server_url:
            raise OAuthConfigError(
                "MCP_OAUTH_ENABLED is true but MCP_SERVER_URL is not set"
            )

        for label, value in (("MCP_OAUTH_ISSUER", self.issuer),
                             ("MCP_SERVER_URL", self.server_url)):
            if not value.startswith(("http://", "https://")):
                raise OAuthConfigError(
                    f"{label} must include a scheme, got {value!r}"
                )

    @property
    def resource_url(self) -> str:
        """The canonical identifier for this protected resource.

        Must equal the URL entered in the connector exactly, path included. A
        mismatch here is a top cause of a flow that starts and then fails.
        """
        return f"{self.server_url}{self.mcp_path}"

    @property
    def metadata_url(self) -> str:
        return f"{self.server_url}{PROTECTED_RESOURCE_PATH}"


class TokenVerifier:
    """Validates JWT access tokens against the issuer's JWKS.

    Discovery is resolved lazily rather than at startup, so this server can come
    up before the identity provider when both are containers starting in an
    arbitrary order.
    """

    def __init__(self, config: OAuthConfig):
        self._config = config
        self._lock = threading.Lock()
        self._jwks: PyJWKClient | None = None
        self._issuer_document: dict[str, Any] | None = None
        self._resolved_at = 0.0

    def _fresh(self) -> bool:
        return (
            self._jwks is not None
            and (time.monotonic() - self._resolved_at) < self._config.jwks_cache_ttl
        )

    def _resolve(self) -> PyJWKClient:
        with self._lock:
            if self._fresh():
                return self._jwks  # type: ignore[return-value]

            discovery_url = (
                f"{self._config.issuer}/.well-known/openid-configuration"
            )

            try:
                response = httpx.get(
                    discovery_url, timeout=DISCOVERY_TIMEOUT_SECONDS
                )
                response.raise_for_status()
                document = response.json()

                self._jwks = PyJWKClient(document["jwks_uri"], cache_keys=True)
                self._issuer_document = document
                self._resolved_at = time.monotonic()
                logger.info(
                    "OAUTH_DISCOVERY_SUCCEEDED issuer=%s resource=%s",
                    self._config.issuer, self._config.resource_url,
                )
            except Exception as exc:
                # A stale verifier beats refusing every request while the
                # provider is briefly unreachable.
                if self._jwks is not None:
                    logger.warning(
                        "OAUTH_DISCOVERY_REFRESH_FAILED issuer=%s error=%s",
                        self._config.issuer, exc,
                    )
                    return self._jwks

                logger.error(
                    "OAUTH_DISCOVERY_FAILED issuer=%s error=%s",
                    self._config.issuer, exc,
                )
                raise

            return self._jwks

    def issuer_document(self) -> dict[str, Any]:
        """The issuer's own discovery document, for mirroring."""
        self._resolve()

        if self._issuer_document is None:  # pragma: no cover - defensive
            raise RuntimeError("issuer metadata unavailable")

        return self._issuer_document

    def verify(self, token: str) -> dict[str, Any]:
        """Validate signature, issuer, expiry, and optionally audience.

        PyJWT does not inspect the `typ` header, so RFC 9068 `at+jwt` access
        tokens validate with no special handling.
        """
        signing_key = self._resolve().get_signing_key_from_jwt(token).key
        audience = self._config.audience

        return jwt.decode(
            token,
            signing_key,
            algorithms=["RS256", "ES256"],
            issuer=self._config.issuer,
            audience=audience or None,
            options={
                "verify_signature": True,
                "verify_exp": True,
                "verify_iss": True,
                "verify_aud": bool(audience),
            },
        )


def _bearer_token(scope: dict[str, Any]) -> str | None:
    """Pull a bearer token out of the ASGI headers.

    Only the Authorization header. A token in a query parameter is prohibited by
    the MCP authorization spec and is not accepted here even as a fallback.
    """
    for raw_name, raw_value in scope.get("headers", []):
        if raw_name.lower() != b"authorization":
            continue

        value = raw_value.decode("latin-1").strip()
        prefix, _, token = value.partition(" ")

        if prefix.lower() == "bearer" and token.strip():
            return token.strip()

    return None


async def _send_json(send, status: int, payload: dict[str, Any],
                     headers: list[tuple[bytes, bytes]] | None = None) -> None:
    body = json.dumps(payload).encode()

    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
            *(headers or []),
        ],
    })
    await send({"type": "http.response.body", "body": body})


class AuthMiddleware:
    """Pure ASGI gate over the MCP endpoint.

    Pure ASGI rather than BaseHTTPMiddleware so the 401 is emitted before any
    MCP framing happens, and so long-lived SSE responses are not buffered.
    """

    def __init__(self, app, config: OAuthConfig, verifier: TokenVerifier):
        self.app = app
        self.config = config
        self.verifier = verifier

    def _challenge_header(self, error: str = "", description: str = "") -> bytes:
        parts = [
            f'resource_metadata="{self.config.metadata_url}"',
            f'scope="{" ".join(REQUIRED_SCOPES)}"',
        ]

        if error:
            parts.append(f'error="{error}"')
            parts.append(f'error_description="{description}"')

        return ("Bearer " + ", ".join(parts)).encode()

    async def _challenge(self, send, error: str = "unauthorized",
                         description: str = "authentication required") -> None:
        await _send_json(
            send, 401,
            {"error": error, "error_description": description},
            [(b"www-authenticate", self._challenge_header(error, description))],
        )

    async def _forbidden(self, send) -> None:
        await _send_json(send, 403, {
            "error": "insufficient_scope",
            "error_description": (
                "your account is not in a group permitted to use this server"
            ),
        })

    async def __call__(self, scope, receive, send) -> None:
        # Health and discovery stay outside the gate.
        if scope["type"] != "http" or not scope["path"].startswith(
            self.config.mcp_path
        ):
            await self.app(scope, receive, send)
            return

        token = _bearer_token(scope)
        if not token:
            await self._challenge(send)
            return

        try:
            claims = self.verifier.verify(token)
        except Exception as exc:
            # The token itself is never logged; a rejected one is still a
            # replayable credential until it expires.
            logger.warning("OAUTH_TOKEN_REJECTED error=%s", exc)
            await self._challenge(
                send, "invalid_token", "the access token is not valid"
            )
            return

        permission = self.config.groups.evaluate(
            groups_from_claims(claims, self.config.groups.claim)
        )
        subject = claims.get("sub")

        if permission == PERMISSION_NONE:
            # Valid token, no mapped group. 403 rather than 401 — the caller is
            # authenticated, and re-authenticating changes nothing.
            logger.warning(
                "AUTHZ_DENIED subject=%s claim=%s",
                subject, self.config.groups.claim,
            )
            await self._forbidden(send)
            return

        # Info, not debug. Who was granted what access is an audit record, and
        # it is worthless if it only appears once someone thinks to raise the
        # log level after the fact.
        logger.info("AUTHZ_GRANTED subject=%s permission=%s", subject, permission)

        scope[SCOPE_PERMISSION_KEY] = permission
        await self.app(scope, receive, send)


def build_routes(config: OAuthConfig, verifier: TokenVerifier) -> list:
    """Unauthenticated discovery routes to register alongside the MCP endpoint."""
    from starlette.routing import Route

    async def protected_resource(request: Request) -> Response:
        return JSONResponse({
            "resource": config.resource_url,
            "authorization_servers": [config.issuer],
            "bearer_methods_supported": ["header"],
            "scopes_supported": list(REQUIRED_SCOPES),
        })

    async def authorization_server(request: Request) -> Response:
        """Mirror the issuer's discovery document from this origin.

        Keeps discovery working for providers whose issuer sits on a nonstandard
        path, and costs nothing for those that do not.
        """
        try:
            return JSONResponse(verifier.issuer_document())
        except Exception as exc:
            logger.error("OAUTH_ISSUER_MIRROR_FAILED error=%s", exc)
            return JSONResponse(
                {"error": "identity provider unavailable"}, status_code=502
            )

    return [
        # Claude probes the path-suffixed location first, then the bare one.
        # Serving both skips a round trip and a class of silent failure.
        Route(f"{PROTECTED_RESOURCE_PATH}{config.mcp_path}",
              protected_resource, methods=["GET"]),
        Route(PROTECTED_RESOURCE_PATH, protected_resource, methods=["GET"]),
        Route(AUTHORIZATION_SERVER_PATH, authorization_server, methods=["GET"]),
    ]
