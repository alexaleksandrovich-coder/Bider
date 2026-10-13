"""Fernet encryption of WB cabinet tokens at rest.

The key comes from `Settings.fernet_key` (BIDER_FERNET_KEY in .env) unless an
explicit key is passed. Ciphertexts are stored as text in cabinets.token_encrypted.
"""

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings


def _fernet(key: str | None) -> Fernet:
    if key is None:
        key = get_settings().fernet_key
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError) as exc:
        raise ValueError(f"invalid Fernet key: {exc}") from exc


def encrypt_token(s: str, key: str | None = None) -> str:
    """Encrypt a token, returning a text ciphertext."""
    return _fernet(key).encrypt(s.encode()).decode()


def decrypt_token(s: str, key: str | None = None) -> str:
    """Decrypt a ciphertext produced by encrypt_token."""
    try:
        return _fernet(key).decrypt(s.encode()).decode()
    except InvalidToken as exc:
        raise ValueError(
            "token decryption failed: wrong Fernet key or corrupted ciphertext"
        ) from exc
