"""Locally signed Auth0-shaped access tokens and a JWKS stand-in (no tenant contact)."""

import base64
import json
import time
from typing import Any

from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.hashes import SHA256

from flakepilot.integrations.infrastructure.auth0 import Auth0TokenVerifier

DOMAIN = "flakepilot-test.us.auth0.com"
AUDIENCE = "https://api.flakepilot.example"
SUBJECT = "agentclient123@clients"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _jwks() -> bytes:
    numbers = KEY.public_key().public_numbers()
    jwk = {
        "kty": "RSA",
        "kid": "k1",
        "use": "sig",
        "n": _b64(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8)),
        "e": _b64(numbers.e.to_bytes(3)),
    }
    return json.dumps({"keys": [jwk]}).encode()


def mint(
    *,
    subject: str = SUBJECT,
    scope: str = "repo.view repo.investigate",
    audience: str = AUDIENCE,
    expires_in: float = 600,
    key: rsa.RSAPrivateKey = KEY,
) -> str:
    now = time.time()
    claims: dict[str, Any] = {
        "iss": f"https://{DOMAIN}/",
        "sub": subject,
        "aud": audience,
        "iat": now - 10,
        "exp": now + expires_in,
        "scope": scope,
        "gty": "client-credentials",
    }
    header = {"alg": "RS256", "typ": "JWT", "kid": "k1"}
    signing_input = (
        f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(claims).encode())}"
    )
    signature = key.sign(signing_input.encode(), padding.PKCS1v15(), SHA256())
    return f"{signing_input}.{_b64(signature)}"


def tampered(token: str) -> str:
    head, claims, signature = token.split(".")
    return f"{head}.{claims}.{('B' if signature[0] == 'A' else 'A') + signature[1:]}"


def local_verifier() -> Auth0TokenVerifier:
    """The real verifier; only the tenant's key-set fetch is replaced."""
    return Auth0TokenVerifier(domain=DOMAIN, audience=AUDIENCE, fetch=lambda _: _jwks())
