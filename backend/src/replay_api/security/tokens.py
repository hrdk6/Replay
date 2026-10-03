"""Random tokens, hashing and HMAC signing.

API keys look like ``rk_<8 hex prefix>_<43 char secret>``. The prefix is
stored in clear (shown in the UI, used for lookup); only the SHA-256 of the
full key is stored. Keys carry 256 bits of randomness, so a fast hash is
appropriate - slow hashes (bcrypt/argon2) exist for low-entropy passwords.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any

API_KEY_PREFIX = "rk_"


def random_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True)
class NewApiKey:
    full_key: str
    prefix: str
    key_hash: str


def generate_api_key() -> NewApiKey:
    prefix = secrets.token_hex(4)
    secret = secrets.token_urlsafe(32)
    full = f"{API_KEY_PREFIX}{prefix}_{secret}"
    return NewApiKey(full, prefix, sha256_hex(full))


def parse_api_key(value: str) -> str | None:
    """Return the lookup prefix if ``value`` is shaped like one of our keys."""
    if not value.startswith(API_KEY_PREFIX) or len(value) > 200:
        return None
    rest = value[len(API_KEY_PREFIX) :]
    prefix, sep, secret = rest.partition("_")
    if not sep or len(prefix) != 8 or len(secret) < 20:
        return None
    try:
        int(prefix, 16)
    except ValueError:
        return None
    return prefix


def constant_time_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def hmac_hex(key: bytes, message: str) -> str:
    return hmac.new(key, message.encode(), hashlib.sha256).hexdigest()


def sign_payload(key: bytes, payload: dict[str, Any], ttl_seconds: int) -> str:
    """Compact signed, expiring token (used for OAuth state and labeling)."""
    body = dict(payload, exp=int(time.time()) + ttl_seconds)
    raw = base64.urlsafe_b64encode(json.dumps(body, separators=(",", ":")).encode()).decode().rstrip("=")
    return f"{raw}.{hmac_hex(key, raw)}"


def verify_payload(key: bytes, token: str) -> dict[str, Any] | None:
    raw, sep, sig = token.partition(".")
    if not sep or not constant_time_equal(hmac_hex(key, raw), sig):
        return None
    try:
        body = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(body, dict) or int(body.get("exp", 0)) < time.time():
        return None
    return body
