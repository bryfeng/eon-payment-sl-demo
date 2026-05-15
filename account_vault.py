"""Encrypted storage helpers for EON base-layer account JSON."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from typing import Any

try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:  # pragma: no cover - exercised only when dependency is absent.
    Fernet = None  # type: ignore[assignment]
    InvalidToken = Exception  # type: ignore[assignment,misc]


class AccountVaultError(RuntimeError):
    """Raised when account key material cannot be encrypted or decrypted."""


def vault_configured() -> bool:
    return bool(os.environ.get("EON_KEY_ENCRYPTION_SECRET")) and Fernet is not None


def _fernet() -> Fernet:
    if Fernet is None:
        raise AccountVaultError(
            "account vault dependency is missing; install cryptography"
        )

    secret = os.environ.get("EON_KEY_ENCRYPTION_SECRET")
    if not secret:
        raise AccountVaultError("EON_KEY_ENCRYPTION_SECRET is required")

    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
    return Fernet(key)


def encrypt_account_json(account_json: dict[str, Any]) -> str:
    plaintext = json.dumps(
        account_json,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return _fernet().encrypt(plaintext).decode("ascii")


def decrypt_account_json(encrypted_account_json: str) -> dict[str, Any]:
    try:
        plaintext = _fernet().decrypt(encrypted_account_json.encode("ascii"))
    except InvalidToken as e:
        raise AccountVaultError("stored base-layer account cannot be decrypted") from e

    try:
        value = json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise AccountVaultError("stored base-layer account JSON is invalid") from e

    if not isinstance(value, dict):
        raise AccountVaultError("stored base-layer account JSON must be an object")
    return value
