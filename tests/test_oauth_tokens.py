"""validate_access_token against real signed tokens.

The other OAuth tests replace validate_access_token with a fake; these drive
the real PyJWT verification path with RS256 tokens and a JWKS served from
memory, including the key-confusion forgeries PyJWT's advisories describe.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException

import app.oauth as oauth

ISSUER = "https://issuer.example"
AUDIENCE = "https://memory.example"


def _key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


SIGNING_KEY = _key()
OTHER_KEY = _key()


def _jwks(private_key, kid: str = "k1") -> dict:
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    return {"keys": [{**jwk, "kid": kid, "use": "sig", "alg": "RS256"}]}


@pytest.fixture(autouse=True)
def oauth_env(monkeypatch):
    monkeypatch.setenv("JARVIS_OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("JARVIS_OIDC_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("JARVIS_OIDC_JWKS_URL", f"{ISSUER}/jwks")
    # The real PyJWKClient, with its JWKS fetch answered from memory.
    client = jwt.PyJWKClient(f"{ISSUER}/jwks")
    monkeypatch.setattr(client, "fetch_data", lambda: _jwks(SIGNING_KEY))
    monkeypatch.setattr(oauth, "_jwk_client", lambda url: client)


def _claims(**overrides) -> dict:
    now = int(time.time())
    return {"iss": ISSUER, "aud": AUDIENCE, "sub": "user-1", "iat": now,
            "exp": now + 300, "scope": "memory.read memory.write", **overrides}


def _token(claims: dict | None = None, key=SIGNING_KEY, kid: str = "k1") -> str:
    return jwt.encode(claims or _claims(), key, algorithm="RS256", headers={"kid": kid})


def _rejected(token: str, status: int = 401) -> None:
    with pytest.raises(HTTPException) as exc:
        oauth.validate_access_token(token)
    assert exc.value.status_code == status


def test_valid_token_yields_principal():
    principal = oauth.validate_access_token(_token())
    assert principal.subject == "user-1"
    assert principal.scopes == frozenset({"memory.read", "memory.write"})
    assert principal.issuer == ISSUER


def test_expired_token_is_rejected():
    _rejected(_token(_claims(exp=int(time.time()) - 60)))


def test_wrong_audience_or_issuer_is_rejected():
    _rejected(_token(_claims(aud="https://someone-else.example")))
    _rejected(_token(_claims(iss="https://evil.example")))


def test_token_from_another_key_with_the_same_kid_is_rejected():
    _rejected(_token(key=OTHER_KEY))


def test_missing_required_claim_is_rejected():
    claims = _claims()
    del claims["sub"]
    _rejected(_token(claims))


def test_missing_scope_is_forbidden():
    _rejected(_token(_claims(scope="other.scope")), status=403)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _hs256_forged_with(secret: bytes) -> str:
    """An HS256 token whose HMAC secret is the server's *public* key."""
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": "k1"}).encode())
    payload = _b64(json.dumps(_claims()).encode())
    signature = hmac.new(secret, f"{header}.{payload}".encode(), hashlib.sha256).digest()
    return f"{header}.{payload}.{_b64(signature)}"


@pytest.mark.parametrize("encoding", ["pem", "pem-crlf", "der"])
def test_hs256_signed_with_the_public_key_is_rejected(encoding):
    """Algorithm confusion: the public key is not an HMAC secret, in any form."""
    public = SIGNING_KEY.public_key()
    if encoding == "der":
        secret = public.public_bytes(serialization.Encoding.DER,
                                     serialization.PublicFormat.SubjectPublicKeyInfo)
    else:
        secret = public.public_bytes(serialization.Encoding.PEM,
                                     serialization.PublicFormat.SubjectPublicKeyInfo)
        if encoding == "pem-crlf":
            secret = secret.replace(b"\n", b"\r\n")
    _rejected(_hs256_forged_with(secret))


def test_unsigned_alg_none_token_is_rejected():
    header = _b64(json.dumps({"alg": "none", "typ": "JWT", "kid": "k1"}).encode())
    payload = _b64(json.dumps(_claims()).encode())
    _rejected(f"{header}.{payload}.")


def test_garbage_token_is_rejected():
    _rejected("not-a-jwt")
