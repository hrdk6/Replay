"""Envelope encryption for provider keys.

Each secret gets its own random 256-bit data-encryption key (DEK). The secret
is encrypted with the DEK (AES-256-GCM), and the DEK is encrypted ("wrapped")
with the key-encryption key (KEK) from ``ENCRYPTION_KEY``. Rotating the KEK
only requires re-wrapping DEKs; ``ENCRYPTION_KEY_PREVIOUS`` is accepted for
unwrapping during rotation.

The KEK could be swapped for a cloud KMS by replacing ``_wrap``/``_unwrap``.
Associated data binds each ciphertext to its org and row so a ciphertext
copied to another org's row fails to decrypt.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_NONCE = 12
_VERSION = b"\x01"


class DecryptionError(Exception):
    pass


@dataclass(frozen=True)
class Sealed:
    ciphertext: bytes  # version || nonce || AES-GCM(secret)
    wrapped_dek: bytes  # version || nonce || AES-GCM(dek)


def _encrypt(key: bytes, plaintext: bytes, aad: bytes) -> bytes:
    nonce = os.urandom(_NONCE)
    return _VERSION + nonce + AESGCM(key).encrypt(nonce, plaintext, aad)


def _decrypt(key: bytes, blob: bytes, aad: bytes) -> bytes:
    if len(blob) < 1 + _NONCE + 16 or blob[:1] != _VERSION:
        raise DecryptionError("malformed ciphertext")
    nonce, body = blob[1 : 1 + _NONCE], blob[1 + _NONCE :]
    return AESGCM(key).decrypt(nonce, body, aad)


def seal(kek: bytes, secret: str, aad: str) -> Sealed:
    dek = AESGCM.generate_key(bit_length=256)
    ad = aad.encode()
    return Sealed(_encrypt(dek, secret.encode(), ad), _encrypt(kek, dek, ad))


def unseal(keks: list[bytes], sealed: Sealed, aad: str) -> str:
    ad = aad.encode()
    last: Exception | None = None
    for kek in keks:
        try:
            dek = _decrypt(kek, sealed.wrapped_dek, ad)
            return _decrypt(dek, sealed.ciphertext, ad).decode()
        except Exception as exc:
            last = exc
    raise DecryptionError("unable to decrypt provider key") from last


def provider_key_aad(org_id: object, key_id: object) -> str:
    return f"provider_key:{org_id}:{key_id}"
