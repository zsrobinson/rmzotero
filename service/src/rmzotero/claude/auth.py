"""Host-specific credentials, checked before the MCP transport sees a request."""

import hmac

import anyio
import jwt
from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse

from ..config import AccessConfig


class ConnectorAuth:
    def __init__(self, app, secret: str, access: AccessConfig, legacy_enabled: bool, hosts: list[str]):
        self.app = app
        self.secret = secret
        self.access = access
        self.legacy_enabled = legacy_enabled
        self.hosts = {h.lower() for h in hosts}
        self.jwks = jwt.PyJWKClient(
            f"{access.team_domain}/cdn-cgi/access/certs", cache_jwk_set=True,
            cache_keys=False, lifespan=300, timeout=5,
        ) if access.enabled else None

    def validate(self, token: str) -> dict:
        if len(token) > 16384 or jwt.get_unverified_header(token).get("alg") != "RS256":
            raise jwt.InvalidTokenError()
        key = self.jwks.get_signing_key_from_jwt(token)
        return jwt.decode(token, key.key, algorithms=["RS256"], audience=self.access.audience,
                          issuer=self.access.team_domain, leeway=30,
                          options={"require": ["aud", "iss", "exp"]})

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        hosts = headers.getlist("host")
        # Do not trust Forwarded or X-Forwarded-Host. Reject duplicate/malformed hosts.
        host = hosts[0].lower() if len(hosts) == 1 else ""
        parts = host.split(":")
        host = parts[0] if len(parts) == 1 or (len(parts) == 2 and parts[1].isdigit()) else ""
        status = 404
        if host not in self.hosts:
            status = 404
        elif scope["path"].startswith("/pdf-download/"):
            # The download handler verifies its own HMAC capability. Keep Host
            # validation, but do not require an interactive Access JWT here.
            return await self.app(scope, receive, send)
        elif host in self.access.hostnames:
            if scope["path"] == "/mcp":
                tokens = headers.getlist("cf-access-jwt-assertion")
                try:
                    if len(tokens) != 1 or not tokens[0]:
                        raise jwt.InvalidTokenError()
                    claims = await anyio.to_thread.run_sync(self.validate, tokens[0])
                except (jwt.PyJWTError, ValueError, TypeError, OSError):
                    status = 401
                else:
                    if self.access.allowed_emails and claims.get("email") not in self.access.allowed_emails:
                        status = 403
                    else:
                        return await self.app(scope, receive, send)
        elif (self.legacy_enabled and self.secret
              and hmac.compare_digest(scope["path"].encode(), f"/mcp/{self.secret}".encode())):
            scope = dict(scope, path="/mcp", raw_path=b"/mcp")
            return await self.app(scope, receive, send)
        await PlainTextResponse("Unauthorized" if status == 401 else "Forbidden" if status == 403 else "Not found",
                                status_code=status)(scope, receive, send)
