"""OAuth 2.1 resource-server support for the public MCP endpoint.

This module deliberately implements the resource-server half only.  A managed
OIDC provider is the authorization server: it owns login, consent, PKCE, token
issuance, key rotation, and client registration.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

import jwt
from fastapi import HTTPException

from app.identity import Principal, normalized_subject


READ_SCOPE = "memory.read"
WRITE_SCOPE = "memory.write"


def auth_mode() -> str:
    return (os.getenv("JARVIS_AUTH_MODE") or "operator").strip().lower()


def oauth_issuer() -> str | None:
    issuer = (os.getenv("JARVIS_OIDC_ISSUER") or "").strip().rstrip("/")
    return issuer or None


def oauth_audience() -> str | None:
    audience = (os.getenv("JARVIS_OIDC_AUDIENCE") or "").strip()
    return audience or None


def oauth_jwks_url() -> str | None:
    return (os.getenv("JARVIS_OIDC_JWKS_URL") or "").strip() or None


def oauth_configured() -> bool:
    return bool(oauth_issuer() and oauth_audience() and oauth_jwks_url())


def public_base_url() -> str:
    configured = (os.getenv("JARVIS_PUBLIC_BASE_URL") or "").strip().rstrip("/")
    if configured:
        return configured
    host = (os.getenv("RENDER_EXTERNAL_HOSTNAME") or "").strip()
    return f"https://{host}" if host else ""


def supported_scopes() -> list[str]:
    return [READ_SCOPE, WRITE_SCOPE]


def protected_resource_metadata() -> dict[str, Any]:
    base = public_base_url()
    issuer = oauth_issuer()
    return {
        "resource": base,
        "authorization_servers": [issuer] if issuer else [],
        "scopes_supported": supported_scopes(),
        "resource_documentation": f"{base}/docs" if base else None,
        "resource_policy_uri": f"{base}/privacy" if base else None,
        "resource_tos_uri": f"{base}/terms" if base else None,
    }


@lru_cache(maxsize=4)
def _jwk_client(url: str) -> jwt.PyJWKClient:
    return jwt.PyJWKClient(url, cache_keys=True, lifespan=300)


def _scopes(claims: dict[str, Any]) -> frozenset[str]:
    value = claims.get("scope", claims.get("scp", []))
    if isinstance(value, str):
        return frozenset(item for item in value.split() if item)
    if isinstance(value, list):
        return frozenset(str(item) for item in value if isinstance(item, str))
    return frozenset()


def validate_access_token(token: str, *, required_scope: str = READ_SCOPE) -> Principal:
    if not oauth_configured():
        raise HTTPException(status_code=503, detail="OAuth is not configured")
    try:
        key = _jwk_client(oauth_jwks_url() or "").get_signing_key_from_jwt(token).key
        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256", "ES256"],
            audience=oauth_audience(),
            issuer=oauth_issuer(),
            options={"require": ["exp", "iat", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail="Invalid or expired OAuth access token") from exc
    subject = normalized_subject(claims.get("sub"))
    if subject is None:
        raise HTTPException(status_code=401, detail="OAuth access token has no valid subject")
    scopes = _scopes(claims)
    if required_scope not in scopes:
        raise HTTPException(status_code=403, detail=f"OAuth access token lacks required scope: {required_scope}")
    return Principal(subject=subject, scopes=scopes, issuer=oauth_issuer() or "")
